"""验证业务枚举重构继续读写既有协议，并保留模型参数允许值。"""

import json
from pathlib import Path

import pytest

from app.agent.context._budget import TokenEstimateSource
from app.agent.models.contracts import ModelFinishReason
from app.agent.state.messages import ImageContent, ToolMessage, message_from_storage_format
from app.agent.tools.read_file import read_file
from app.agent.tools.terminal.shell import ShellKind
from app.agent.tools.update_plan import update_plan
from app.schemas.mcp import MCPServerStatus, MCPTransport
from app.schemas.message_types import MessageType, MediaKind, HistoryRole
from app.schemas.runtime_types import CheckpointType
from app.schemas.tool_types import ToolSource, AuthorizationTiming, PlanItemStatus, FileReadUnit, PatchKind


def test_business_values_serialize_with_existing_protocol_strings() -> None:
    """独立固定样例锁定持久化和接口字符串，避免枚举名称进入协议。"""
    expected = json.loads((Path(__file__).parent / "fixtures/business-protocol-values.json").read_text(encoding="utf-8"))
    enum_types = (MessageType, MediaKind, HistoryRole, ToolSource,
                  AuthorizationTiming, PlanItemStatus, FileReadUnit, PatchKind, CheckpointType,
                  MCPServerStatus,
                  MCPTransport, ModelFinishReason, TokenEstimateSource, ShellKind)
    values = {enum_type.__name__: list(enum_type) for enum_type in enum_types}
    assert json.loads(json.dumps(values)) == expected
    assert {name: [str(member) for member in members] for name, members in values.items()} == expected


@pytest.mark.parametrize("kind", ["human", "tool", "mcp", "environment"])
def test_image_storage_restores_enums_from_existing_strings(kind: str) -> None:
    """旧字符串图片经过消息存储往返后恢复业务枚举，存储字段仍为字符串。

    Args:
        kind (str): 固定样例中的图片来源。
    """
    message = ToolMessage(tool_name="read_image", tool_call_id="call",
                          content=(ImageContent("data:image/png;base64,AA==", media_kind=kind),))
    stored = json.loads(json.dumps(message.to_storage_format()))
    assert stored["content"][0]["media_kind"] == kind
    restored = message_from_storage_format(stored).content[0]
    assert restored.media_kind is MediaKind(kind)


def test_tool_schemas_keep_existing_allowed_values() -> None:
    """实际注册的工具参数继续约束原允许值，枚举的维护说明不会进入模型参数。"""
    unit = read_file.parameters_schema["properties"]["unit"]
    assert unit["enum"] == ["line", "character"]
    assert unit["default"] == "line"
    item = update_plan.parameters_schema["properties"]["items"]["items"]
    assert item["properties"]["status"]["enum"] == ["pending", "in_progress", "completed"]
