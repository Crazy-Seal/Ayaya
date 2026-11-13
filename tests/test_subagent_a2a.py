"""父子显式消息、原工具回复及停止竞争的行为测试。"""
from app.crud.sqlite_factory import SqliteFactory

import asyncio
from pathlib import Path

import pytest

from app.agent.models.contracts import StreamChunk
from app.agent.models.contracts import ProjectedMessage
from app.agent.state.messages import ToolCall, ToolMessage, HumanMessage
from app.schemas.subagent_types import SubagentAction, SubagentPublicStatus
from tests.subagent_support import SubagentHarness, wait_for_subagent_worker, subagent_worker_running, wait_for_subagent_status


@pytest.mark.parametrize("running", [False, True])
def test_command_replay_observes_existing_activity_without_starting_or_steering(tmp_path: Path, running: bool) -> None:
    """相同来源重放只返回原受理结果，执行中和已完成时都不产生额外执行。

    Args:
        tmp_path (Path): 隔离存储目录。
        running (bool): 重放时原命令是否仍在执行。
    """
    async def scenario() -> None:
        """通过公开回执、模型输入和结果历史观察整项命令去重。"""
        started, release = asyncio.Event(), asyncio.Event()
        inputs = []

        async def script(messages, tools):
            """第二项任务等待测试放行，重复来源不能取消它。"""
            inputs.append(messages)
            if len(inputs) == 2:
                started.set()
                await release.wait()
            yield StreamChunk(content="已完成")

        async with SubagentHarness(tmp_path / "state.sqlite3", script) as harness:
            initial = await harness.create()
            await asyncio.wait_for(wait_for_subagent_worker(harness.scheduler, initial.context_id), 3)
            command = await harness.scheduler.interact_subagent("parent", initial.context_id,
                SubagentAction.COMMAND, "第二项任务", source_id="fixed-source")
            await asyncio.wait_for(started.wait(), 3)
            if not running:
                release.set()
                await asyncio.wait_for(wait_for_subagent_worker(harness.scheduler, command.context_id), 3)
            replay = await harness.scheduler.interact_subagent("parent", initial.context_id,
                SubagentAction.COMMAND, "第二项任务", source_id="fixed-source")
            assert replay.accepted and replay.status == command.status
            if running:
                assert subagent_worker_running(harness.scheduler, replay.context_id)
                release.set()
                await asyncio.wait_for(wait_for_subagent_worker(harness.scheduler, command.context_id), 3)
            else:
                assert not subagent_worker_running(harness.scheduler, replay.context_id)
                assert (await harness.scheduler.get_subagent_details("parent", replay.context_id))["status"] == SubagentPublicStatus.IDLE
            details = await harness.scheduler.get_subagent_details("parent", initial.context_id)
            assert len(inputs) == 2
            assert len(details["results"]) == 2
            assert all(item["status"] == "completed" for item in details["results"])

    asyncio.run(scenario())


@pytest.mark.parametrize("action", [SubagentAction.COMMAND, SubagentAction.STOP, SubagentAction.DESTROY])
@pytest.mark.parametrize("retain", [False, True])
def test_a2a_wait_is_replied_or_cancelled_once(tmp_path: Path, action: SubagentAction, retain: bool) -> None:
    """等待暂停子执行；父命令作为新指令，停止或销毁结束委派。

    Args:
        tmp_path (Path): 隔离存储目录。
        action (SubagentAction): 本例对等待采取的操作。
        retain (bool): 是否保留子上下文，同时覆盖一次性和可复用的 SQL 状态保存。
    """
    async def scenario() -> None:
        """观察真实工具结果和已保存交互，不访问 Scheduler 内部字段。"""
        answers: list[str] = []
        async def script(messages, tools):
            """首次提问，原工具完成后观察父回复再输出报告。"""
            result = next((message for message in messages if isinstance(message, ToolMessage)), None)
            if result is None:
                yield StreamChunk(tool_call=ToolCall(id="question", name="message_parent",
                                                     args={"text": "要处理哪个文件？"}))
            else:
                assert result.text == "消息已投递。"
                commands = [message.message if isinstance(message, ProjectedMessage) else message for message in messages]
                answers.append(next(message.text for message in reversed(commands) if isinstance(message, HumanMessage)))
                yield StreamChunk(content="收到父回复，任务完成")

        async with SubagentHarness(tmp_path / "state.sqlite3", script, retain=retain) as harness:
            receipt = await harness.create()
            await asyncio.wait_for(wait_for_subagent_status(harness.scheduler, receipt.context_id, SubagentPublicStatus.WAITING_PARENT), 3)
            summaries = await harness.scheduler.snapshot_subagents("parent")
            assert summaries[0].status == SubagentPublicStatus.WAITING_PARENT
            assert subagent_worker_running(harness.scheduler, receipt.context_id)
            response = await harness.scheduler.interact_subagent("parent", receipt.context_id, action,
                                                                 "处理 app.py" if action == SubagentAction.COMMAND else None)
            assert response.accepted
            await asyncio.wait_for(wait_for_subagent_worker(harness.scheduler, receipt.context_id), 3)
            detail = await harness.scheduler.get_subagent_details("parent", receipt.context_id)
            if action == SubagentAction.COMMAND:
                assert len(answers) == 1 and answers[0].endswith("处理 app.py")
                assert [item["kind"] for item in detail["recent_interactions"]] == ["task", "a2a", "task", "final"]
            else:
                assert not answers
                assert detail["results"][0]["status"] == "stopped"
            assert detail["context_available"] == (retain and action != SubagentAction.DESTROY)
    asyncio.run(scenario())


def test_fast_parent_command_cancels_remaining_batch_and_keeps_delegation(tmp_path: Path) -> None:
    """父看到已提交问题立即引导时，旧批次后续工具取消，下一轮收到父正文。"""
    async def scenario() -> None:
        """在父收到问题时回复，不等待子状态通知或任何专用等待对象。"""
        from app.agent.tools.core.base import BaseTool, ToolResult
        from app.agent.state.manager import SessionStateManager
        from app.schemas.agent_storage import AgentStorageScope

        calls = []
        class LaterTool(BaseTool):
            """记录旧批次后续工具是否错误执行。"""
            name, description = 'later_tool', '执行后续操作'
            parameters_schema = {'type': 'object', 'properties': {}}

            async def execute(self, args, context) -> ToolResult:
                """本例的旧批次后续操作应由引导取消。"""
                calls.append('unexpected')
                return ToolResult.success('执行了')

        async def script(messages, tools):
            """先提交问题及后续工具，新指令轮只产生最终回答。"""
            if not any(isinstance(message, ToolMessage) for message in messages):
                yield StreamChunk(tool_call=ToolCall('question', 'message_parent', {'text': '如何继续？'}))
                yield StreamChunk(tool_call=ToolCall('later', 'later_tool', {}))
            else:
                commands = [item.message if isinstance(item, ProjectedMessage) else item for item in messages]
                assert next(item.text for item in reversed(commands) if isinstance(item, HumanMessage)).endswith('使用新方法')
                yield StreamChunk(content='已按新方法完成')

        async with SubagentHarness(tmp_path / 'state.sqlite3', script, retain=False, tools=(LaterTool(),)) as harness:
            receipt = await harness.create()
            question = await asyncio.wait_for(harness.received.get(), 3)
            assert question[0].payload['text'] == '如何继续？'
            await harness.scheduler.interact_subagent('parent', receipt.context_id, SubagentAction.COMMAND, '使用新方法')
            await asyncio.wait_for(wait_for_subagent_worker(harness.scheduler, receipt.context_id), 3)
            detail = await harness.scheduler.get_subagent_details('parent', receipt.context_id)
            assert len(detail['results']) == 1
            assert detail['results'][0]['result'] == '已按新方法完成'
            assert calls == []
            reader = SessionStateManager(receipt.context_id, SqliteFactory(harness.path), storage_scope=AgentStorageScope.SUBAGENT)
            try:
                state = await reader.load()
                results = {item.tool_call_id: item.text for item in state.messages if isinstance(item, ToolMessage)}
                assert results['question'] == '消息已投递。'
                assert '取消' in results['later']
                assert state.long_term_memory_turn_counter == 0
            finally:
                await reader.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("action", [SubagentAction.STOP, SubagentAction.DESTROY])
@pytest.mark.parametrize("reply_first", [False, True])
def test_reply_racing_with_stop_or_destroy_records_at_most_one_reply(
    tmp_path: Path, action: SubagentAction, reply_first: bool,
) -> None:
    """并发回复及停止或销毁只解决原等待一次，最终报告保持唯一。

    Args:
        tmp_path (Path): 隔离数据库目录。
        action (SubagentAction): 与父回复竞争的停止或销毁。
        reply_first (bool): 两个并发请求进入事件循环的先后顺序。
    """
    async def scenario() -> None:
        """使用公开交互命令并发受理，不依赖具体锁或 Future 实现。"""
        observed: list[str] = []

        async def script(messages, tools):
            """若原工具正常返回，只能观察到一次父回复。"""
            result = next((message for message in messages if isinstance(message, ToolMessage)), None)
            if result is None:
                yield StreamChunk(tool_call=ToolCall(id="question", name="message_parent", args={"text": "处理什么？"}))
            else:
                assert result.text == "消息已投递。"
                commands = [message.message if isinstance(message, ProjectedMessage) else message for message in messages]
                observed.append(next(message.text for message in reversed(commands) if isinstance(message, HumanMessage)))
                yield StreamChunk(content="处理完成")

        async with SubagentHarness(tmp_path / "state.sqlite3", script) as harness:
            receipt = await harness.create()
            await asyncio.wait_for(wait_for_subagent_status(harness.scheduler, receipt.context_id, SubagentPublicStatus.WAITING_PARENT), 3)
            reply = harness.scheduler.interact_subagent("parent", receipt.context_id, SubagentAction.COMMAND, "处理 app.py")
            stop = harness.scheduler.interact_subagent("parent", receipt.context_id, action)
            operations = (reply, stop) if reply_first else (stop, reply)
            results = await asyncio.wait_for(asyncio.gather(*operations), 3)
            reply_receipt = results[0 if reply_first else 1]
            await asyncio.wait_for(wait_for_subagent_worker(harness.scheduler, receipt.context_id), 3)
            detail = await harness.scheduler.get_subagent_details("parent", receipt.context_id)
            replies = [item for item in detail["recent_interactions"] if item["kind"] == "reply"]
            assert not replies
            assert len(observed) <= 1
            if observed:
                assert observed[0].endswith("处理 app.py")
            assert len(detail["results"]) == 1
            assert len([item for item in detail["recent_interactions"] if item["kind"] == "final"]) == 1
    asyncio.run(scenario())
