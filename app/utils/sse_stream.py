"""为独立运行时订阅提供 JSON SSE 帧、空闲心跳和订阅清理。"""

import asyncio
import json
from dataclasses import asdict
from typing import Any, AsyncIterator

from app.agent.state.messages import to_json_value


SSE_HEARTBEAT_SECONDS = 60.0


async def stream_sse_json(
    source: AsyncIterator[Any], *, heartbeat_interval: float = SSE_HEARTBEAT_SECONDS,
) -> AsyncIterator[str]:
    """持续消费领域事件，空闲时发送不进入业务序号的注释心跳。

    Args:
        source (AsyncIterator[Any]): 产生 dataclass 事件或桌面执行请求的订阅。
        heartbeat_interval (float): 无数据时发送心跳的间隔秒数，默认每分钟一次。

    Yields:
        str: 完整 JSON data 帧或不包含业务载荷的注释心跳帧。

    Raises:
        TypeError: 领域事件不能转换为 JSON 数据时终止本次订阅。
        Exception: 来源订阅产生的真实异常继续传播给连接处理器。
    """
    iterator = source.__aiter__()
    pending = asyncio.ensure_future(anext(iterator))
    try:
        while True:
            # 1. 心跳只观察等待时间，不能用 wait_for 取消来源的 queue.get 并丢失订阅。
            done, _ = await asyncio.wait({pending}, timeout=heartbeat_interval)
            if not done:
                # 1.1. 注释帧只维持响应体活跃，不发布 EventHub 事件，也不逐帧写日志。
                yield ": ping\n\n"
                continue
            try:
                item = pending.result()
            except StopAsyncIteration:
                return
            pending = None
            # 2. 递归解冻工具参数，时间戳沿用既有字符串格式，避免内部对象地址进入 UI。
            yield "data: " + json.dumps(
                to_json_value(asdict(item)), ensure_ascii=False, default=str,
            ) + "\n\n"
            pending = asyncio.ensure_future(anext(iterator))
    finally:
        # 3. 连接退出只取消订阅读取并关闭来源，不触碰 Scheduler 的 Agent 或工具生命周期。
        if pending is not None:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        close = getattr(iterator, "aclose", None)
        if close is not None:
            await close()
