"""验证保存与关闭共用的尝试上限、等待预算及取消传播。"""

import asyncio
import gc
import logging
import weakref

import pytest

from app.agent.utils.infra.bounded_operation import OperationTimeoutError, run_bounded


def test_fourth_save_attempt_can_succeed() -> None:
    """前三次保存暂时失败时，第四次仍可提交相同结果。"""
    async def scenario() -> None:
        """记录实际保存调用，确认业务材料只准备一次。"""
        attempts: list[object] = []
        prepared_result = object()

        async def save() -> object:
            """重用同一份材料，模拟前三次存储暂时不可用。

            Returns:
                object: 已提交的原始材料。
            Raises:
                OSError: 前三次存储暂时不可用。
            """
            attempts.append(prepared_result)
            if len(attempts) < 4:
                raise OSError("暂时无法保存")
            return prepared_result

        result = await run_bounded(save, retryable=(OSError,), timeout_seconds=1, retry_delay_seconds=0)
        assert result is prepared_result
        assert attempts == [prepared_result] * 4

    asyncio.run(scenario())


@pytest.mark.parametrize("failures", [0, 4])
def test_success_or_exhaustion_ends_attempts(failures: int) -> None:
    """首次成功立即结束，连续四次失败传播最后一次原始错误。

    Args:
        failures (int): 连续失败的次数。
    """
    async def scenario() -> None:
        """记录实际调用及错误身份，验证尝试结束条件。"""
        calls: list[OSError] = []

        async def save() -> str:
            """模拟同一事务的成功或连续失败。

            Returns:
                str: 提交成功标记。
            Raises:
                OSError: 已配置为可重试的临时保存故障。
            """
            error = OSError(f"第 {len(calls) + 1} 次失败")
            calls.append(error)
            if len(calls) <= failures:
                raise error
            return "已保存"

        if failures:
            with pytest.raises(OSError) as caught:
                await run_bounded(save, retryable=(OSError,), timeout_seconds=1, retry_delay_seconds=0)
            assert len(calls) == 4
            assert caught.value is calls[-1]
        else:
            assert await run_bounded(save, retryable=(OSError,), timeout_seconds=1) == "已保存"
            assert len(calls) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("error", [ValueError("材料无效"), asyncio.CancelledError()])
def test_unconfigured_error_and_operation_cancellation_propagate(error: BaseException) -> None:
    """不可重试错误及操作自身取消只执行一次，并保留原始错误。

    Args:
        error (BaseException): 注入的参数错误或操作取消。
    """
    async def scenario() -> None:
        """验证指定错误类别之外的退出路径。"""
        calls = 0

        async def save() -> None:
            """抛出预先指定的错误。

            Raises:
                BaseException: 本场景注入的错误或取消。
            """
            nonlocal calls
            calls += 1
            raise error

        with pytest.raises(type(error)) as caught:
            await run_bounded(save, retryable=(OSError,), timeout_seconds=1)
        assert caught.value is error
        assert calls == 1

    asyncio.run(scenario())


def test_budget_includes_retry_delays_and_preserves_last_error() -> None:
    """重试间隔耗尽总预算后结束，不能重新发放每次尝试的预算。"""
    async def scenario() -> None:
        """用长于总预算的间隔验证下一次保存不会启动。"""
        calls = 0
        error = OSError("存储暂时不可用")

        async def save() -> None:
            """模拟第一次临时失败。

            Raises:
                OSError: 存储暂时不可用。
            """
            nonlocal calls
            calls += 1
            raise error

        with pytest.raises(OperationTimeoutError) as caught:
            await run_bounded(save, retryable=(OSError,), timeout_seconds=0.02, retry_delay_seconds=1)
        assert caught.value.__cause__ is error
        assert calls == 1

    asyncio.run(scenario())


def test_timeout_ends_wait_while_operation_finishes_cancellation() -> None:
    """外部资源延迟响应取消时，总预算仍能结束调用者的等待。"""
    async def scenario() -> None:
        """以事件控制资源退出，证明超时独立于实际关闭完成。"""
        cancelled, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
        calls = 0

        async def close_resource() -> None:
            """收到取消后等待外部资源确认退出。"""
            nonlocal calls
            calls += 1
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
            finally:
                finished.set()

        try:
            with pytest.raises(OperationTimeoutError):
                await asyncio.wait_for(run_bounded(close_resource, retryable=(OSError,), timeout_seconds=0.02), 1)
            await asyncio.wait_for(cancelled.wait(), 1)
            assert not finished.is_set()
            assert calls == 1
        finally:
            release.set()
            await asyncio.wait_for(finished.wait(), 1)

    asyncio.run(scenario())


def test_timed_out_attempt_remains_alive_until_actual_exit() -> None:
    """超时后仍在异步收尾的操作保留到实际退出，再释放资源引用。"""
    async def scenario() -> None:
        """用垃圾回收验证操作所有权，并显式结束本测试的延迟退出。"""
        cancelled, finished = asyncio.Event(), asyncio.Event()
        attempt_ref: weakref.ReferenceType[asyncio.Task[object]] | None = None

        async def close_resource() -> None:
            """收到取消后继续等外部退出，第二次取消才完成本次模拟。"""
            nonlocal attempt_ref
            current = asyncio.current_task()
            assert current is not None
            attempt_ref = weakref.ref(current)
            try:
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    await asyncio.Event().wait()
            finally:
                finished.set()

        with pytest.raises(OperationTimeoutError):
            await run_bounded(close_resource, retryable=(OSError,), timeout_seconds=0.02)
        await asyncio.wait_for(cancelled.wait(), 1)
        gc.collect()
        assert attempt_ref is not None
        remaining = attempt_ref()
        assert remaining is not None
        assert not finished.is_set()
        remaining.cancel()
        with pytest.raises(asyncio.CancelledError):
            await remaining
        await asyncio.wait_for(finished.wait(), 1)
        del remaining
        settled = asyncio.Event()
        asyncio.get_running_loop().call_soon(settled.set)
        await settled.wait()
        gc.collect()
        assert attempt_ref() is None

    asyncio.run(scenario())


def test_caller_cancel_interrupts_attempt_without_retry() -> None:
    """调用方取消等待时传播取消，请求当前尝试结束并停止重试。"""
    async def scenario() -> None:
        """通过操作开始和退出事件控制取消顺序。"""
        started, finished = asyncio.Event(), asyncio.Event()
        calls = 0

        async def save() -> None:
            """等待存储返回，取消时确认已经退出。"""
            nonlocal calls
            calls += 1
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                finished.set()

        waiter = asyncio.create_task(run_bounded(save, retryable=(OSError,), timeout_seconds=1))
        await asyncio.wait_for(started.wait(), 1)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        await asyncio.wait_for(finished.wait(), 1)
        assert calls == 1

    asyncio.run(scenario())


def test_observer_cancel_preserves_owned_save() -> None:
    """观察者结束等待时，调用契约保护的同一次保存仍可完成。"""
    async def scenario() -> None:
        """在保存已开始后取消观察者，再确认原保存完成一次。"""
        started, release = asyncio.Event(), asyncio.Event()
        calls = 0

        async def save() -> str:
            """等待允许提交后返回本次结果。

            Returns:
                str: 已完成的保存结果。
            """
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return "已保存"

        owned = asyncio.create_task(run_bounded(save, retryable=(OSError,), timeout_seconds=1))

        async def observe() -> str:
            """按观察契约等待已归属业务执行方的保存。

            Returns:
                str: 原保存结果，取消观察时由 shield 保留原任务。
            """
            return await asyncio.shield(owned)

        observer = asyncio.create_task(observe())
        try:
            await asyncio.wait_for(started.wait(), 1)
            observer.cancel()
            with pytest.raises(asyncio.CancelledError):
                await observer
            assert not owned.done()
        finally:
            release.set()
        assert await owned == "已保存"
        assert calls == 1

    asyncio.run(scenario())


def test_resource_owner_only_retries_unclosed_resources() -> None:
    """关闭方法记录局部进度，后一个资源失败时前一个只关闭一次。"""
    async def scenario() -> None:
        """以资源持有者的一次关闭调用驱动有限重试。"""
        pending = ["模型流", "模型客户端"]
        calls: list[str] = []

        async def close_remaining() -> None:
            """按持有者保存的剩余资源继续关闭。

            Raises:
                OSError: 模型客户端前三次暂时无法关闭。
            """
            while pending:
                resource = pending[0]
                calls.append(resource)
                if resource == "模型客户端" and calls.count(resource) < 4:
                    raise OSError("关闭暂时失败")
                pending.pop(0)

        await run_bounded(close_remaining, retryable=(OSError,), timeout_seconds=1, retry_delay_seconds=0)
        assert not pending
        assert calls == ["模型流", "模型客户端", "模型客户端", "模型客户端", "模型客户端"]

    asyncio.run(scenario())


def test_close_owner_logs_exhaustion_and_continues_other_resources(caplog: pytest.LogCaptureFixture) -> None:
    """资源持有者记录关闭耗尽，继续关闭其他资源。

    Args:
        caplog (pytest.LogCaptureFixture): 捕获资源持有者的最终错误日志。
    """
    async def scenario() -> None:
        """示范关闭与保存分别处理相同有限工具的错误。"""
        calls: list[str] = []

        async def close_first() -> None:
            """模拟某个资源持续关闭失败。

            Raises:
                OSError: 外部资源始终未确认关闭。
            """
            calls.append("first")
            raise OSError("资源关闭失败")

        async def close_second() -> None:
            """关闭持有者的另一个资源。"""
            calls.append("second")

        for close in (close_first, close_second):
            try:
                await run_bounded(close, retryable=(OSError,), timeout_seconds=1, retry_delay_seconds=0)
            except (OSError, OperationTimeoutError):
                logging.getLogger(__name__).exception("资源关闭尝试已耗尽")
        assert calls == ["first", "first", "first", "first", "second"]

    asyncio.run(scenario())
    assert len(caplog.records) == 1
