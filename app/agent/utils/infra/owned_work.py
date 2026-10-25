"""让已取得所有权的工作完成后，再向调用者传播取消。"""

import asyncio
from collections.abc import Coroutine
from typing import Any, TypeVar

T = TypeVar("T")


async def complete_owned_work(operation: Coroutine[Any, Any, T]) -> T:
    """保持内部工作身份，重复取消外层等待也须等到实际操作完成。

    Args:
        operation (Coroutine[Any, Any, T]): 已开始负责的登记、清理或线程工作。

    Returns:
        T: 未收到取消时，返回内部操作的实际结果。

    Raises:
        asyncio.CancelledError: 内部操作完成后传播外层取消，或内部操作自身取消。
        Exception: 内部操作的实际错误。
    """
    work = asyncio.create_task(operation)
    cancelled = False
    # 1. 每次外层取消只中断本次等待，内部工作继续占有原来的资源。
    while not work.done():
        try:
            await asyncio.shield(work)
        except asyncio.CancelledError:
            if work.cancelled():
                raise
            cancelled = True
    # 2. 先取得真实结果或错误，再把延期的取消交给调用者。
    result = work.result()
    if cancelled:
        raise asyncio.CancelledError
    return result
