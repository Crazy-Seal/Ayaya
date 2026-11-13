"""为已经准备好的保存事务或资源关闭提供有限尝试和总等待预算。"""

import asyncio
import math
import logging
from collections.abc import Awaitable, Callable
from typing import TypeVar


T = TypeVar("T")
MAX_OPERATION_ATTEMPTS = 4
DEFAULT_RETRY_DELAY_SECONDS = 0.05
# 保留已收到取消、仍在实际退出的尝试；完成回调领取结果后移除。
_cancelling_attempts: set[asyncio.Task[object]] = set()


class OperationTimeoutError(TimeoutError):
    """总等待预算耗尽，调用方据此报告保存失败或记录关闭错误。"""


def _consume_finished_attempt(task: asyncio.Task[T]) -> None:
    """读取超时或取消后的尝试结果，释放异步异常记录。

    Args:
        task (asyncio.Task[T]): 已被请求取消、随后才退出的单次操作。
    """
    _cancelling_attempts.discard(task)
    if not task.cancelled():
        task.exception()


def _cancel_attempt(task: asyncio.Task[T]) -> None:
    """请求单次尝试退出，并持有它直到异步收尾完成。

    Args:
        task (asyncio.Task[T]): 总预算耗尽或调用者取消时尚未结束的操作。
    """
    _cancelling_attempts.add(task)
    task.add_done_callback(_consume_finished_attempt)
    task.cancel()


async def run_bounded(
    operation: Callable[[], Awaitable[T]],
    *,
    retryable: tuple[type[Exception], ...],
    timeout_seconds: float,
    retry_delay_seconds: float = DEFAULT_RETRY_DELAY_SECONDS,
) -> T:
    """在同一总预算内尝试操作，首次失败后最多再尝试三次。

    保存方传入已准备好材料的事务；资源持有者传入可重复的关闭步骤，
    在自己的关闭方法中记录最终错误。多个资源共用关闭预算时，持有者
    逐次传入剩余秒数，并记住已经关闭的资源。模型、工具和事件发布由
    原业务流程执行。取消传播给当前尝试；必要保存的取消保护由调用契约决定。
    超时只结束等待并请求取消，外部操作响应取消后才实际退出。

    Args:
        operation (Callable[[], Awaitable[T]]): 每次尝试相同事务或关闭步骤的工厂。
        retryable (tuple[type[Exception], ...]): 允许重试的具体错误类别，空元组表示尝试一次。
        timeout_seconds (float): 所有尝试及间隔共用的正数总预算，单位为秒。
        retry_delay_seconds (float): 两次尝试之间的非负等待秒数。

    Returns:
        T: 首次成功的操作结果。

    Raises:
        ValueError: 预算或间隔超出允许范围。
        OperationTimeoutError: 总预算耗尽，当前尝试收到取消请求。
        asyncio.CancelledError: 调用者或操作取消，本次操作随之结束。
        Exception: 不可重试错误或第四次尝试的原始错误。
    """
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("总预算必须为有限正数")
    if not math.isfinite(retry_delay_seconds) or retry_delay_seconds < 0:
        raise ValueError("重试间隔必须为有限非负数")

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    last_error: Exception | None = None

    async def attempt_operation() -> T:
        """在独立任务中执行一次保存或关闭。

        Returns:
            T: 此次尝试的结果，异常由等待方按配置处理。
        """
        return await operation()

    # 1. 每次尝试及等待都扣除同一个总预算。
    for attempt in range(MAX_OPERATION_ATTEMPTS):
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise OperationTimeoutError("操作总等待预算已耗尽") from last_error
        task = asyncio.create_task(attempt_operation())
        try:
            done, _ = await asyncio.wait({task}, timeout=remaining)
        except asyncio.CancelledError:
            _cancel_attempt(task)
            raise

        # 2. 超时请求取消后立即结束等待，后续只领取结束结果。
        if not done:
            _cancel_attempt(task)
            raise OperationTimeoutError("操作总等待预算已耗尽") from last_error

        # 3. 仅指定错误可重试，成功、取消及其他错误直接返回或传播。
        try:
            return task.result()
        except retryable as exc:
            if attempt == MAX_OPERATION_ATTEMPTS - 1:
                raise
            last_error = exc
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise OperationTimeoutError("操作总等待预算已耗尽") from exc
            if retry_delay_seconds >= remaining:
                await asyncio.sleep(remaining)
                raise OperationTimeoutError("操作总等待预算已耗尽") from exc
            await asyncio.sleep(retry_delay_seconds)

    raise AssertionError("有限尝试必须返回结果或抛出错误")


async def close_resources(
    resources: tuple[tuple[str, Callable[[], Awaitable[None]]], ...], *,
    logger: logging.Logger, timeout_seconds: float = 5.0,
    retryable: tuple[type[Exception], ...] = (OSError,),
) -> None:
    """在资源所有者的关闭方法内逐项释放，耗尽时记日志并继续其他资源。

    Args:
        resources (tuple): 资源名称及可安全重复的关闭步骤。
        logger (logging.Logger): 资源所有者的日志出口。
        timeout_seconds (float): 本次所有资源共用的等待预算。
        retryable (tuple[type[Exception], ...]): 所有者确认允许重复的关闭错误。
    """
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    for name, close in resources:
        try:
            await run_bounded(close, retryable=retryable,
                timeout_seconds=max(0.001, deadline - asyncio.get_running_loop().time()))
        except Exception:
            logger.exception("释放资源失败：%s", name)
