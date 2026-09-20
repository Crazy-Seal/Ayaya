from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from datetime import timedelta

import pytest

from app.agent.state import AgentState
from app.agent.state.messages import (
    AssistantMessage,
    AvailableMedia,
    BaseMessage,
    CompactedMedia,
    ContextSummaryMessage,
    EnvironmentPerceptionMessage,
    HumanMessage,
    MessageContractError,
    RuntimeInputMessage,
    SystemMessage,
    TaskResultMessage,
    ToolCall,
    ToolMediaMessage,
    ToolMessage,
    message_from_protocol_format,
    message_from_storage_format,
)


def test_domain_message_round_trip_keeps_type_and_hides_metadata_from_wire() -> None:
    message = TaskResultMessage(
        task_id="task-1",
        task_name="索引代码",
        status="completed",
        result="完成",
    )

    wire = message.to_openai_format()
    stored = message.to_storage_format()
    restored = message_from_storage_format(stored)

    assert wire == {
        "role": "user",
        "name": "runtime_task_result",
        "content": "[后台任务完成] 索引代码\n完成",
    }
    assert not any(key.startswith("_") for key in wire)
    assert stored["_message_type"] == "task_result"
    assert stored["_task"] == {
        "task_id": "task-1",
        "task_name": "索引代码",
        "status": "completed",
    }
    assert restored == message


def test_screenshot_payload_is_closed_and_compaction_never_rehydrates_image() -> None:
    available = ToolMediaMessage(
        tool_name="screenshot",
        media_kind="screenshot",
        text="[系统消息]工具 screenshot 返回图片: ",
        payload=AvailableMedia(
            urls=("data:image/png;base64,QQ==",),
            storage_refs=("screenshot/shot.png",),
        ),
        associated_message_id="tool-message-1",
    )
    compacted = replace(
        available,
        payload=CompactedMedia(storage_refs=("screenshot/shot.png",)),
    )

    restored = message_from_storage_format(compacted.to_storage_format())

    assert isinstance(restored, ToolMediaMessage)
    assert isinstance(restored.payload, CompactedMedia)
    assert restored.to_openai_format() == {
        "role": "user",
        "name": "system_screenshot_compressed",
        "content": "[系统消息]已被压缩的旧截图",
    }
    assert "data:image" not in str(restored.to_openai_format())


def test_agent_state_exposes_snapshot_and_closes_tool_batch_semantically() -> None:
    state = AgentState.create_new("message-contract")
    first = ToolCall(id="call-a", name="screenshot", args={})
    second = ToolCall(id="call-b", name="read_file", args={})
    state.record_assistant_step("", (first, second))

    state.record_tool_result(
        first,
        "已截图",
        compressible=True,
        image_urls=("data:image/png;base64,QQ==",),
        image_message_name="system_screenshot",
    )
    assert [type(message) for message in state.messages] == [
        AssistantMessage,
        # 第一条工具消息已提交，但图片要等整批闭合。
        type(state.messages[1]),
    ]

    state.record_tool_result(second, "文件内容", compressible=True)

    snapshot = state.messages
    assert isinstance(snapshot, tuple)
    assert isinstance(snapshot[-1], ToolMediaMessage)
    assert state.pending_tool_calls == ()
    with pytest.raises(AttributeError):
        snapshot.append(HumanMessage(text="绕过接口"))  # type: ignore[attr-defined]
    with pytest.raises(FrozenInstanceError):
        snapshot[0].message_id = "changed"  # type: ignore[misc]


def test_runtime_input_subclasses_have_distinct_storage_discriminators() -> None:
    task = TaskResultMessage(
        task_id="task-1",
        task_name="任务",
        status="failed",
        result="失败",
    )
    environment = EnvironmentPerceptionMessage(
        perception_kind="window_changed",
        description="前台窗口已变化",
    )

    assert task.message_type == "task_result"
    assert environment.message_type == "environment_perception"
    assert task.to_storage_format()["_message_type"] != (
        environment.to_storage_format()["_message_type"]
    )


def test_closed_discriminators_reject_unknown_values_at_construction() -> None:
    with pytest.raises(ValueError, match="媒体类型"):
        ToolMediaMessage(
            tool_name="capture",
            media_kind="camera",  # type: ignore[arg-type]
            text="image",
            payload=AvailableMedia(urls=("data:image/png;base64,QQ==",)),
        )
    with pytest.raises(ValueError, match="只有截图"):
        ToolMediaMessage(
            tool_name="draw",
            media_kind="tool",
            text="image",
            payload=CompactedMedia(),
        )
    with pytest.raises(ValueError, match="任务状态"):
        TaskResultMessage(
            task_id="task-1",
            task_name="任务",
            status="running",  # type: ignore[arg-type]
            result="pending",
        )


def test_tool_call_arguments_are_deeply_immutable() -> None:
    call = ToolCall(
        id="call-1",
        name="read_file",
        args={"paths": ["a.py"], "nested": {"enabled": True}},
        extra_content={"provider": {"trace": ["one"]}},
    )
    before = call.to_dict()

    with pytest.raises(TypeError, match="immutable"):
        call.args["path"] = "b.py"  # type: ignore[index]
    with pytest.raises((TypeError, AttributeError)):
        del call.args["paths"]  # type: ignore[attr-defined]
    for method_name in ("update", "pop", "popitem", "clear", "setdefault"):
        with pytest.raises(AttributeError):
            getattr(call.args, method_name)
    frozen = call.args
    with pytest.raises(TypeError):
        frozen |= {"new": True}
    with pytest.raises(TypeError, match="immutable"):
        call.args.__init__({"changed": True})  # type: ignore[misc]
    with pytest.raises(TypeError, match="immutable"):
        call.args._view = {}  # type: ignore[attr-defined]
    with pytest.raises(AttributeError):
        call.args["paths"].append("b.py")  # type: ignore[union-attr]
    with pytest.raises(TypeError, match="immutable"):
        call.args["nested"]["enabled"] = False  # type: ignore[index]
    with pytest.raises(TypeError, match="immutable"):
        call.extra_content["provider"]["trace"] = ()  # type: ignore[index]

    assert deepcopy(call).to_dict() == before
    assert call.to_dict() == before


def test_abstract_message_types_cannot_be_instantiated() -> None:
    with pytest.raises(TypeError, match="abstract"):
        BaseMessage()  # type: ignore[abstract]
    with pytest.raises(TypeError, match="abstract"):
        RuntimeInputMessage()  # type: ignore[abstract]


def test_tool_message_requires_non_empty_call_id_in_all_decoders() -> None:
    with pytest.raises((TypeError, ValueError)):
        ToolMessage(content="结果", tool_name="read_file")  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="tool_call_id"):
        ToolMessage(content="结果", tool_name="read_file", tool_call_id="")

    valid = ToolMessage(
        content="结果", tool_name="read_file", tool_call_id="call-1"
    )
    stored = valid.to_storage_format()
    stored.pop("tool_call_id")
    with pytest.raises(ValueError, match="tool_call_id"):
        message_from_storage_format(stored)
    with pytest.raises(ValueError, match="tool_call_id"):
        message_from_protocol_format(
            message_type="tool",
            message_json={"role": "tool", "name": "read_file", "content": "结果"},
            message_id="tool-1",
            created_at=valid.created_at,
            domain_json={"tool": {"compressible": False}},
        )


def test_decoders_report_missing_or_illegal_message_type_as_contract_error() -> None:
    with pytest.raises(MessageContractError, match="_message_type"):
        message_from_storage_format({})
    with pytest.raises(MessageContractError, match="message_type"):
        message_from_protocol_format(
            message_type=None,
            message_json={"role": "user", "content": "问题"},
            message_id="human-1",
            created_at=HumanMessage(text="时间锚点").created_at,
        )
    with pytest.raises(MessageContractError, match="非法"):
        message_from_protocol_format(
            message_type="system",
            message_json={"role": "system", "content": "不应进入协议日志"},
            message_id="system-1",
            created_at=HumanMessage(text="时间锚点").created_at,
        )


def _restore_state(
    messages,
    *,
    pending=(),
    pending_actions=(),
    interrupt_data=None,
) -> AgentState:
    state = AgentState.create_new("contract")
    checkpoint = state.to_checkpoint()
    checkpoint["messages"] = [message.to_storage_format() for message in messages]
    checkpoint["pending_tool_calls"] = [call.to_dict() for call in pending]
    checkpoint["pending_actions"] = [call.to_dict() for call in pending_actions]
    checkpoint["interrupt_data"] = interrupt_data
    return AgentState.from_checkpoint(checkpoint)


def test_agent_state_rejects_invalid_tool_protocol_sequences() -> None:
    call = ToolCall(id="call-1", name="read_file", args={})
    assistant = AssistantMessage(text="", tool_calls=(call,))
    result = ToolMessage(
        content="结果", tool_name="read_file", tool_call_id="call-1"
    )

    with pytest.raises(ValueError, match="找不到"):
        _restore_state((result,))
    with pytest.raises(ValueError, match="名称不匹配"):
        _restore_state((
            assistant,
            replace(result, tool_name="write_file"),
        ))
    with pytest.raises(ValueError, match="多个或乱序"):
        _restore_state((assistant, result, replace(result, message_id="result-2")))
    with pytest.raises(ValueError, match="非 ToolMessage 打断"):
        _restore_state((assistant, HumanMessage(text="插入"), result))
    with pytest.raises(ValueError, match="重复工具调用 ID"):
        _restore_state((AssistantMessage(text="", tool_calls=(call, call)),))


def test_agent_state_requires_unclosed_batch_to_match_recovery_state_exactly() -> None:
    first = ToolCall(id="call-1", name="read_file", args={})
    second = ToolCall(id="call-2", name="write_file", args={})
    third = ToolCall(id="call-3", name="list_files", args={})
    assistant = AssistantMessage(text="", tool_calls=(first, second, third))
    first_result = ToolMessage(
        content="完成", tool_name="read_file", tool_call_id="call-1"
    )
    interrupt = {
        "tool_call_id": "call-2",
        "tool_name": "write_file",
        "type": "approval",
    }

    active = _restore_state((assistant, first_result), pending=(second, third))
    assert active.pending_tool_calls == (second, third)

    restored = _restore_state(
        (assistant, first_result),
        pending_actions=(third,),
        interrupt_data=interrupt,
    )
    assert restored.interrupt_data == interrupt
    with pytest.raises(ValueError, match="工具协议未闭合"):
        _restore_state((assistant, first_result))
    with pytest.raises(ValueError, match="名称不匹配"):
        _restore_state((
            assistant,
            first_result,
        ), pending_actions=(third,), interrupt_data={**interrupt, "tool_name": "wrong"})
    with pytest.raises(ValueError, match="已闭合或不存在"):
        _restore_state((assistant, first_result), pending=(first, second, third))
    with pytest.raises(ValueError, match="必须依附 interrupt"):
        _restore_state((assistant, first_result), pending_actions=(second, third))
    with pytest.raises(ValueError, match="缺少 tool_call_id"):
        _restore_state(
            (assistant, first_result),
            pending_actions=(third,),
            interrupt_data={"type": "approval", "tool_name": "write_file"},
        )
    with pytest.raises(ValueError, match="缺少 tool_name"):
        _restore_state(
            (assistant, first_result),
            pending_actions=(third,),
            interrupt_data={"type": "approval", "tool_call_id": "call-2"},
        )
    with pytest.raises(ValueError, match="不得同时保留 pending_tool_calls"):
        _restore_state(
            (assistant, first_result),
            pending=(second, third),
            pending_actions=(third,),
            interrupt_data=interrupt,
        )


def test_agent_state_rejects_system_messages_and_compaction_insertion_abuse() -> None:
    state = AgentState.create_new("contract")
    with pytest.raises(ValueError, match="SystemMessage"):
        state.commit_compaction((SystemMessage(text="系统提示"),))

    human = state.add_user_message("问题")
    assistant = state.add_assistant_message("回答")
    with pytest.raises(ValueError, match="只能新增 ContextSummaryMessage"):
        state.commit_compaction((*state.messages, HumanMessage(text="绕过输入接口")))
    with pytest.raises(ValueError, match="消息类型"):
        state.commit_compaction((
            AssistantMessage(
                message_id=human.message_id,
                created_at=human.created_at,
                text="伪装",
            ),
            assistant,
        ))
    with pytest.raises(ValueError, match="消息时间"):
        state.commit_compaction((
            replace(human, created_at=human.created_at + timedelta(seconds=1)),
            assistant,
        ))
    with pytest.raises(ValueError, match="相对顺序"):
        state.commit_compaction((assistant, human))

    summary = ContextSummaryMessage(
        content="摘要",
        summary_schema_version=1,
        covered_through_message_id=human.message_id,
        covered_effective_date="2026-09-20",
        compression_ratio=0.5,
    )
    state.commit_compaction((summary, human, replace(assistant, text="压缩后的回答")))
    assert state.messages == (summary, human, replace(assistant, text="压缩后的回答"))
