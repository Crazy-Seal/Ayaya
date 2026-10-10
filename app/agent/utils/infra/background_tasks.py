import asyncio
import logging
from collections.abc import Coroutine
from typing import Any


_BACKGROUND_TASKS: set[asyncio.Task[Any]] = set()


def create_background_task(
    coro: Coroutine[Any, Any, Any], *, logger: logging.Logger, task_name: str
) -> asyncio.Task[Any]:
    """创建受管控的后台任务，统一回收并记录异常。

    Args:
        coro (Coroutine[Any, Any, Any]): 需要交给 asyncio 调度的异步过程。
        logger (logging.Logger): 记录当前工具调用的日志对象。
        task_name (str): 用于任务日志和诊断的名称。
    """
    task = asyncio.create_task(coro)
    _BACKGROUND_TASKS.add(task)

    def _on_done(done_task: asyncio.Task[Any]) -> None:
        """由 asyncio 在后台任务结束时回调，移除强引用并读取结果，取消或失败写入日志。

        Args:
            done_task (asyncio.Task[Any]): asyncio 已结束的后台任务。
        """
        _BACKGROUND_TASKS.discard(done_task)
        try:
            done_task.result()
        except asyncio.CancelledError:
            logger.warning("后台任务被取消: %s", task_name)
        except Exception as e:
            logger.exception("后台任务执行失败: %s", task_name, exc_info=e)

    task.add_done_callback(_on_done)
    return task
