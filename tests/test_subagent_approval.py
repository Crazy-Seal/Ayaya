"""验证同一父会话的多子审批串行展示和实际子上下文恢复。"""
from tests.runtime_support import next_runtime_event

import asyncio
from pathlib import Path

import pytest

from app.agent.context import ContextStrategyConfig
from app.agent.models.contracts import ProjectedMessage, StreamChunk
from app.agent.state.messages import HumanMessage, ToolCall, ToolMessage
from app.agent.tools.authorization.manager import ToolAuthorization
from app.agent.tools.core.base import BaseTool, ToolResult
from app.interaction.interaction_contract import ChannelCommand
from app.schemas.chat_settings import ChatSettings
from app.schemas.runtime_types import CommandKind, InteractionEventKind
from app.schemas.subagent_types import SubagentAction
from tests.subagent_support import SubagentHarness, wait_for_subagent_worker


@pytest.mark.parametrize("failed_step", ["cancel", "close_tools"])
def test_parent_command_retries_only_pause_transactions_without_stale_approval(tmp_path: Path, monkeypatch, failed_step: str) -> None:
    """整理旧审批时由 DAO 重试固定事务，模型执行一次新命令且审批继续展示。

    Args:
        tmp_path (Path): 隔离数据库目录。
        monkeypatch (pytest.MonkeyPatch): 在 DAO 事务写入处注入暂时性保存失败。
        failed_step (str): 取消决定或补齐旧工具的失败位置。
    """
    from app.agent.state.manager import SessionStateManager

    async def scenario() -> None:
        """使用真实审批和父命令，通过第二项审批确认队列能够继续。"""
        settings = ChatSettings(session_id="parent", model_name="test", openai_api_key="test",
            openai_base_url="http://unused", temperature=0, system_prompt="test", context_strategy=ContextStrategyConfig())
        injected = asyncio.Event()
        model_inputs = []
        import sqlite3
        from app.crud.agent_state_dao import AgentStateDao
        from app.schemas.runtime_types import CheckpointType
        original = AgentStateDao._write_state_in_transaction
        attempts = 0
        child_id = None

        async def fail_temporarily(dao, db, state, **kwargs):
            """在真实事务写入处模拟三次 BUSY，第四次保存同一份旧协议。"""
            nonlocal attempts
            interrupt = state.interrupt_data or {}
            chosen = (bool(interrupt.get("runtime_decision")) if failed_step == "cancel"
                      else kwargs.get("checkpoint_type") == CheckpointType.COMPLETED)
            if dao.session_id == child_id and chosen and attempts < 3:
                attempts += 1
                injected.set()
                error = sqlite3.OperationalError("暂停整理暂时占用")
                error.sqlite_errorcode = sqlite3.SQLITE_BUSY
                raise error
            return await original(dao, db, state, **kwargs)

        class ProtectedTool(BaseTool):
            """本场景的旧工具由父命令取消。"""
            name, description = "protected", "需要审批的测试操作"
            parameters_schema = {"type": "object", "properties": {}}

            async def execute(self, args, context) -> ToolResult:
                """被新指令替代的旧审批不能执行。"""
                raise AssertionError("旧审批工具不应执行")

        async def script(messages, tools):
            """收到新命令后完成，其他子首次执行都等待审批。"""
            model_inputs.append(messages)
            history = [item.message if isinstance(item, ProjectedMessage) else item for item in messages]
            if any(isinstance(item, HumanMessage) and item.text.endswith("改做新任务") for item in history):
                yield StreamChunk(content="新任务已完成")
            else:
                yield StreamChunk(tool_call=ToolCall("protected", "protected", {}))

        async with SubagentHarness(tmp_path / "state.sqlite3", script, tools=(ProtectedTool(),),
                authorization=ToolAuthorization(lambda _id: settings)) as harness:
            first = await harness.create()
            while (event := await asyncio.wait_for(next_runtime_event(harness.scheduler), 3)).kind != InteractionEventKind.SUBAGENT_INTERRUPT:
                pass
            child_id = first.context_id
            monkeypatch.setattr(AgentStateDao, "_write_state_in_transaction", fail_temporarily)
            # 1. 受理返回后旧暂停交给原 owner 整理，失败不撤销已保存的命令。
            assert (await harness.scheduler.interact_subagent("parent", first.context_id, SubagentAction.COMMAND,
                "改做新任务", source_id="fixed-command")).accepted
            await asyncio.wait_for(injected.wait(), 3)
            await harness.scheduler.interact_subagent("parent", first.context_id, SubagentAction.COMMAND,
                "改做新任务", source_id="fixed-command")
            await asyncio.wait_for(wait_for_subagent_worker(harness.scheduler, first.context_id), 4)
            assert attempts == 3
            assert len(model_inputs) == 2
            details = await harness.scheduler.get_subagent_details("parent", first.context_id)
            assert len(details["results"]) == 1 and details["results"][0]["status"] == "completed"
            assert (await harness.scheduler.snapshot("parent")).payload["approval_prompt"] is None
            # 2. 下一项审批真正展示，证明旧队首已删除。
            second = await harness.create("第二项任务")
            while True:
                prompt = await asyncio.wait_for(next_runtime_event(harness.scheduler), 3)
                if prompt.kind == InteractionEventKind.SUBAGENT_INTERRUPT:
                    assert prompt.payload["subagent_id"] == second.context_id
                    break

    asyncio.run(asyncio.wait_for(scenario(), 15))


def test_destroy_closes_displayed_approval_and_rejects_late_decision(tmp_path: Path) -> None:
    """废弃提交后立即通知界面关闭原审批，迟到决定不能恢复子执行。"""
    async def scenario() -> None:
        """真实触发工具审批，从公开事件消费与审批命令检查销毁语义。"""
        settings = ChatSettings(session_id="parent", model_name="test", openai_api_key="test",
            openai_base_url="http://unused", temperature=0, system_prompt="test", context_strategy=ContextStrategyConfig())

        class ProtectedTool(BaseTool):
            """销毁之后不会运行的受保护操作。"""
            name, description = "protected", "需要审批的测试操作"
            parameters_schema = {"type": "object", "properties": {}}

            async def execute(self, args, context) -> ToolResult:
                """废弃身份不能恢复到真正的工具调用。"""
                raise AssertionError("销毁后的审批不能执行工具")

        async def script(messages, tools):
            """请求一个会暂停于用户审批的工具。"""
            yield StreamChunk(tool_call=ToolCall(id="approval-call", name="protected", args={}))

        async with SubagentHarness(tmp_path / "state.sqlite3", script, tools=(ProtectedTool(),),
            authorization=ToolAuthorization(lambda _id: settings)) as harness:
            receipt = await harness.create()
            while True:
                event = await asyncio.wait_for(next_runtime_event(harness.scheduler), 3)
                if event.kind == InteractionEventKind.SUBAGENT_INTERRUPT:
                    break
            request_id = event.request_id
            await harness.scheduler.interact_subagent("parent", receipt.context_id, SubagentAction.DESTROY)
            while True:
                event = await asyncio.wait_for(next_runtime_event(harness.scheduler), 3)
                if event.kind == InteractionEventKind.AUTHORIZATION_RESOLVED:
                    break
            assert event.request_id == request_id
            assert event.payload["subagent_id"] == receipt.context_id
            assert event.payload["outcome"] == "cancelled"
            rejected = await harness.scheduler.submit(ChannelCommand("parent", "desktop", CommandKind.RESOLVE_AUTHORIZATION,
                {"kind": "approve"}, request_id=request_id, subagent_id=receipt.context_id))
            assert not rejected.accepted
    asyncio.run(scenario())


def test_child_approvals_are_fifo_and_resolve_in_actual_child_context(tmp_path: Path) -> None:
    """两个子调用默认 ask 工具，只展示队首，批准和拒绝分别写回对应上下文。"""
    async def scenario() -> None:
        """使用实际权限判断、工具协议保存和公开审批命令。"""
        called: list[str] = []
        settings = ChatSettings(session_id="parent", model_name="test", openai_api_key="test",
            openai_base_url="http://unused", temperature=0, system_prompt="test", context_strategy=ContextStrategyConfig())

        class ProtectedTool(BaseTool):
            """记录审批后实际执行的调用。"""
            name, description = "protected", "需要审批的测试操作"
            parameters_schema = {"type": "object", "properties": {}}

            async def execute(self, args, context) -> ToolResult:
                """执行一次获准调用，记录真实工具所属子身份。"""
                called.append(context.runtime.subagent_id)
                return ToolResult.success("批准后执行成功")

        async def script(messages, tools):
            """尚无工具结果时发起调用，恢复后产出最终报告。"""
            if not any(isinstance(item, ToolMessage) for item in messages):
                yield StreamChunk(tool_call=ToolCall(id="protected-call", name="protected", args={}))
            else:
                yield StreamChunk(content="审批后完成")

        async with SubagentHarness(tmp_path / "state.sqlite3", script, tools=(ProtectedTool(),),
                                   authorization=ToolAuthorization(lambda _id: settings)) as harness:
            first = await harness.create()
            async def next_prompt():
                """从公共输出读取下一个子审批事件。"""
                while True:
                    event = await next_runtime_event(harness.scheduler)
                    if event.kind == InteractionEventKind.SUBAGENT_INTERRUPT:
                        return event
            prompt = await asyncio.wait_for(next_prompt(), 3)
            second = await harness.create("第二个子任务")
            # 第二项进入审批不改变队首，快照供断线重连恢复同一个对话框。
            while True:
                summaries = await harness.scheduler.snapshot_subagents("parent")
                if len(summaries) == 2 and all(item.waiting_for_approval for item in summaries):
                    break
                await asyncio.sleep(0.01)
            snapshot = await harness.scheduler.snapshot("parent")
            assert snapshot.payload["approval_prompt"]["subagent_id"] == first.context_id
            approved = await harness.scheduler.submit(ChannelCommand("parent", "desktop", CommandKind.RESOLVE_AUTHORIZATION,
                {"kind": "approve"}, request_id=prompt.request_id, subagent_id=first.context_id))
            assert approved.accepted
            next_request = (await harness.scheduler.snapshot("parent")).payload["approval_prompt"]
            assert next_request["subagent_id"] == second.context_id
            rejected = await harness.scheduler.submit(ChannelCommand("parent", "desktop", CommandKind.RESOLVE_AUTHORIZATION,
                {"kind": "reject"}, request_id=next_request["request_id"], subagent_id=second.context_id))
            assert rejected.accepted
            await asyncio.wait_for(asyncio.gather(wait_for_subagent_worker(harness.scheduler, first.context_id), wait_for_subagent_worker(harness.scheduler, second.context_id)), 3)
            assert called == [first.context_id]
            assert (await harness.scheduler.snapshot("parent")).payload["approval_prompt"] is None
    asyncio.run(asyncio.wait_for(scenario(), 15))


@pytest.mark.parametrize("retain,stop_first", [(False, False), (True, False), (True, True)])
def test_parent_command_closes_cancelled_child_approval(
    tmp_path: Path, retain: bool, stop_first: bool,
) -> None:
    """父命令结束旧审批及未执行调用后继续，停止后的保留型也能再次受派。

    Args:
        tmp_path (Path): 当前用例独占的数据库目录。
        retain (bool): 子任务结束后是否允许再次受派，两类上下文均持久保存。
        stop_first (bool): 是否先停止审批中的保留型，再发送新的父命令。
    """
    async def scenario() -> None:
        """保留真实审批、命令交付及结果保存，仅替换模型和受保护工具。"""
        called: list[str] = []
        observed_results: list[ToolMessage] = []
        settings = ChatSettings(session_id="parent", model_name="test", openai_api_key="test",
            openai_base_url="http://unused", temperature=0, system_prompt="test", context_strategy=ContextStrategyConfig())

        class ProtectedTool(BaseTool):
            """验证取消审批后，原调用和同批剩余调用都不会实际执行。"""
            name, description = "protected", "需要审批的测试操作"
            parameters_schema = {"type": "object", "properties": {}}

            async def execute(self, args, context) -> ToolResult:
                """记录意外执行，以便从行为验证旧调用已被取消。"""
                called.append(context.runtime.subagent_id)
                return ToolResult.success("不应执行的旧操作")

        async def script(messages, tools):
            """首轮请求两次工具调用，新命令到达后检查旧调用结果再完成。"""
            history = [item.message if isinstance(item, ProjectedMessage) else item for item in messages]
            if any(isinstance(item, HumanMessage) and item.text.endswith("改做新的任务") for item in history):
                observed_results.extend(item for item in history if isinstance(item, ToolMessage))
                yield StreamChunk(content="已按新命令完成")
            else:
                yield StreamChunk(tool_call=ToolCall(id="approval-call", name="protected", args={}))
                yield StreamChunk(tool_call=ToolCall(id="unstarted-call", name="protected", args={}))

        async with SubagentHarness(tmp_path / "state.sqlite3", script, retain=retain, tools=(ProtectedTool(),),
                                   authorization=ToolAuthorization(lambda _id: settings)) as harness:
            receipt = await harness.create()
            # 1. 等真实审批请求已登记，确保命令替代的是正在等待真人决定的调用。
            while True:
                event = await next_runtime_event(harness.scheduler)
                if event.kind == InteractionEventKind.SUBAGENT_INTERRUPT:
                    break
            request_id = event.request_id
            if stop_first:
                stopped = await harness.scheduler.interact_subagent("parent", receipt.context_id, SubagentAction.STOP)
                assert stopped.accepted
                await asyncio.wait_for(wait_for_subagent_worker(harness.scheduler, receipt.context_id), 3)

            # 2. 新命令由子 Agent 结束旧工具调用后交付，保持父 Agent 来源。
            command = await harness.scheduler.interact_subagent(
                "parent", receipt.context_id, SubagentAction.COMMAND, "改做新的任务",
            )
            assert command.accepted
            await asyncio.wait_for(wait_for_subagent_worker(harness.scheduler, command.context_id), 3)
            assert called == []
            assert [item.tool_call_id for item in observed_results] == ["approval-call", "unstarted-call"]
            details = await harness.scheduler.get_subagent_details("parent", receipt.context_id)
            result = details["results"][0]
            assert result["result"] == "已按新命令完成"
            assert (await harness.scheduler.snapshot("parent")).payload["approval_prompt"] is None

            # 3. 已取消的审批不能再恢复旧操作，也不会影响新指令的最终结果。
            stale = await harness.scheduler.submit(ChannelCommand("parent", "desktop", CommandKind.RESOLVE_AUTHORIZATION,
                {"kind": "approve"}, request_id=request_id, subagent_id=receipt.context_id))
            assert not stale.accepted
    asyncio.run(asyncio.wait_for(scenario(), 15))
