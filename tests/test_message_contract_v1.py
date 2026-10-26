from dataclasses import replace
from datetime import timedelta
import pytest

from app.agent.state import AgentState, TemporaryContextItem
from app.agent.state.messages import (AssistantMessage, BaseMessage, ContextSummaryMessage, HumanMessage,
    RuntimeInputMessage, SystemMessage, TaskResultMessage, ToolCall, ToolMessage, ProviderData,
    EnvironmentPerceptionMessage,
    TextContent, ImageContent, text_content, message_from_storage_format, MessageContractError, notification_text)
from app.schemas.runtime_types import TaskResultStatus


def test_domain_message_roundtrip_is_independent_of_provider_roles() -> None:
    """所有业务类型按自己的字段往返，不保存供应商角色。"""
    messages = [HumanMessage(content=text_content("你好"), origin_channel="inbox"),
        AssistantMessage(content=text_content("回答")), ToolMessage(content=text_content("结果"), tool_name="read_file", tool_call_id="c"),
        TaskResultMessage(task_id="task", task_name="命令", status=TaskResultStatus.COMPLETED, result="完成")]
    for message in messages:
        stored = message.to_storage_format()
        assert "role" not in stored and message_from_storage_format(stored) == message


def test_temporary_context_restores_from_checkpoint_without_entering_history_messages() -> None:
    """临时条目独立保存到 checkpoint，旧 checkpoint 缺少字段时恢复为空集合。"""
    state = AgentState.create_new("temporary-context")
    state.add_user_message("原始问题")
    state.set_temporary_context("weather.current", TemporaryContextItem(title="天气", text="晴"))
    state.set_temporary_context("weather.current", TemporaryContextItem(title="天气", text="雨"))
    checkpoint = state.to_checkpoint()
    restored = AgentState.from_checkpoint(checkpoint)
    assert restored.temporary_context == {"weather.current": TemporaryContextItem(title="天气", text="雨")}
    assert [message.text for message in restored.messages] == ["原始问题"]
    restored.remove_temporary_context("weather.current")
    assert restored.to_checkpoint()["temporary_context"] == {}
    del checkpoint["temporary_context"]
    assert AgentState.from_checkpoint(checkpoint).temporary_context == {}


def test_environment_message_is_request_only_and_cannot_activate_agent() -> None:
    """环境背景不属于运行时输入，也不能作为持久状态消息追加或恢复。"""
    message = EnvironmentPerceptionMessage(content=text_content("本次请求背景"))
    state = AgentState.create_new("environment")
    assert not message.enters_protocol_log
    with pytest.raises(TypeError, match="Agent 输入"):
        state.append_input(message)
    checkpoint = state.to_checkpoint()
    checkpoint["messages"] = [message.to_storage_format()]
    with pytest.raises(ValueError, match="EnvironmentPerceptionMessage"):
        AgentState.from_checkpoint(checkpoint)


def test_unknown_and_mixed_task_results_keep_business_status() -> None:
    """未知与混合任务终态的通知文字不伪称成功。"""
    for status, label in ((TaskResultStatus.ORPHANED, "状态未知"), (TaskResultStatus.MIXED, "状态汇总")):
        message = TaskResultMessage(task_id="t", task_name="任务", status=status, result="结果")
        assert label in notification_text(message)


def test_tool_parameters_and_provider_material_are_deeply_immutable() -> None:
    """审批后外部字典变化不能改变参数或专用恢复材料。"""
    raw = {"items": ["a"]}
    extra = {"extra_content": {"signature": ["one"]}}
    call = ToolCall("c", "read_file", raw, ProviderData("openai_chat_completions", "https://example", "model", extra))
    raw["items"].append("b")
    extra["extra_content"]["signature"].append("two")
    assert call.to_dict()["args"] == {"items": ["a"]}
    assert call.provider_data.to_dict()["payload"] == {"extra_content": {"signature": ["one"]}}
    with pytest.raises(TypeError):
        call.args["new"] = "bad"


def test_decoders_reject_old_roles_unknown_types_and_missing_call_id() -> None:
    """运行时拒绝旧格式及不完整结果，不执行静默兼容。"""
    for value in ({"role": "user", "content": "旧输入"}, {"message_type": "unknown"}):
        with pytest.raises(MessageContractError):
            message_from_storage_format(value)
    message = ToolMessage(content=text_content("结果"), tool_name="read_file", tool_call_id="c")
    stored = message.to_storage_format()
    stored["tool_call_id"] = ""
    with pytest.raises(MessageContractError):
        message_from_storage_format(stored)


def _restore_state(
    messages,
    *,
    pending=(),
    pending_actions=(),
    interrupt_data=None,
) -> AgentState:
    """将测试消息和工具恢复字段经 checkpoint 格式重新还原为状态。

    Args:
        messages (list[dict]): 按对话顺序排列的消息集合。
        pending (Any): 尚未得到文本结果的工具调用。
        pending_actions (Any): 当前中断之后尚未执行的同批工具调用。
        interrupt_data (Any): 当前等待审批或桌面执行的恢复信息。

    Returns:
        AgentState: 供外层用例断言使用的预设结果。
    """
    state = AgentState.create_new("contract")
    checkpoint = state.to_checkpoint()
    checkpoint["messages"] = [message.to_storage_format() for message in messages]
    checkpoint["pending_tool_calls"] = [call.to_dict() for call in pending]
    checkpoint["pending_actions"] = [call.to_dict() for call in pending_actions]
    checkpoint["interrupt_data"] = interrupt_data
    return AgentState.from_checkpoint(checkpoint)


def test_agent_state_rejects_invalid_tool_protocol_sequences() -> None:
    """验证状态拒绝非法工具请求结果顺序。"""
    call = ToolCall(id="call-1", name="read_file", args={})
    assistant = AssistantMessage(content=text_content(""), tool_calls=(call,))
    result = ToolMessage(
        content=text_content("结果"), tool_name="read_file", tool_call_id="call-1"
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
        _restore_state((assistant, HumanMessage(content=text_content("插入")), result))
    with pytest.raises(ValueError, match="重复工具调用 ID"):
        _restore_state((AssistantMessage(content=text_content(""), tool_calls=(call, call)),))


def test_agent_state_requires_unclosed_batch_to_match_recovery_state_exactly() -> None:
    """验证未闭合工具批次与恢复记录必须精确对应。"""
    first = ToolCall(id="call-1", name="read_file", args={})
    second = ToolCall(id="call-2", name="write_file", args={})
    third = ToolCall(id="call-3", name="list_files", args={})
    assistant = AssistantMessage(content=text_content(""), tool_calls=(first, second, third))
    first_result = ToolMessage(
        content=text_content("完成"), tool_name="read_file", tool_call_id="call-1"
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
    """验证状态拒绝系统提示词和非法压缩插入。"""
    state = AgentState.create_new("contract")
    with pytest.raises(ValueError, match="SystemMessage"):
        state.commit_compaction((SystemMessage(text="系统提示"),))

    human = state.add_user_message("问题")
    assistant = state.add_assistant_message("回答")
    with pytest.raises(ValueError, match="只能新增 ContextSummaryMessage"):
        state.commit_compaction((*state.messages, HumanMessage(content=text_content("绕过输入接口"))))
    with pytest.raises(ValueError, match="消息类型"):
        state.commit_compaction((
            AssistantMessage(
                message_id=human.message_id,
                created_at=human.created_at,
                content=text_content("伪装"),
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
    state.commit_compaction((summary, human, replace(assistant, content=text_content("压缩后的回答"))))
    assert state.messages == (summary, human, replace(assistant, content=text_content("压缩后的回答")))
