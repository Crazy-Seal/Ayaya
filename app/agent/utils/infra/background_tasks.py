import asyncio
import logging
from collections.abc import Coroutine
from typing import Any


_BACKGROUND_TASKS: set[asyncio.Task[Any]] = set()


def create_background_task(
    coro: Coroutine[Any, Any, Any], *, logger: logging.Logger, task_name: str
) -> asyncio.Task[Any]:
    """创建受管控的后台任务，统一回收并记录异常。"""
    task = asyncio.create_task(coro)
    _BACKGROUND_TASKS.add(task)

    def _on_done(done_task: asyncio.Task[Any]) -> None:
        _BACKGROUND_TASKS.discard(done_task)
        try:
            done_task.result()
        except asyncio.CancelledError:
            logger.warning("后台任务被取消: %s", task_name)
        except Exception as e:
            logger.exception("后台任务执行失败: %s", task_name, exc_info=e)

    task.add_done_callback(_on_done)
    return task
