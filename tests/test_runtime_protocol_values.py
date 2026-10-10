"""以固定协议样例检查运行时枚举及 SSE 序列化，不依赖内部状态字段。"""

import asyncio
import json
from pathlib import Path

from app.schemas import runtime_types
from app.services.interaction_contract import InteractionEvent
from app.utils.sse_stream import stream_sse_json


def test_runtime_values_match_fixed_protocol_and_serialize_as_strings() -> None:
    """验证全部分类的值与既有协议一致，嵌套枚举经过真实 SSE 仍是字符串。"""
    fixture = json.loads((Path(__file__).parent / "fixtures/runtime-protocol-values.json").read_text(encoding="utf-8"))
    values = {}
    # 1. 固定样例独立于实现定义，增删或改名必须显式确认协议，而不能随实现自动通过。
    for name, expected in fixture.items():
        enum_type = getattr(runtime_types, name)
        assert [member.value for member in enum_type] == expected
        values[name] = list(enum_type)

    async def scenario() -> None:
        """消费真实序列化帧，确认枚举名称没有泄漏到线协议。"""
        async def source():
            """产生包含所有取值的有限事件，避免依赖真实 Agent 和数据库。

            Yields:
                InteractionEvent: 使用枚举类别和嵌套取值的公共事件。
            """
            yield InteractionEvent("s", runtime_types.InteractionEventKind.SESSION_SNAPSHOT, values)

        frames = [frame async for frame in stream_sse_json(source())]
        received = json.loads(frames[0][6:])
        # 2. 检查线路结果而非私有编码方法，既有读者只会收到原字符串。
        assert received["kind"] == "session_snapshot"
        assert received["payload"] == fixture

    asyncio.run(scenario())
