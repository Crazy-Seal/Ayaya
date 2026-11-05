"""验证取消请求等待只结束本次回执，Scheduler 仍处理已经接管的请求。"""

import asyncio
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

import pytest

from app.agent.models.contracts import StreamChunk, ToolDefinition
from app.agent.runtime.requests import RuntimeRequest
from app.agent.runtime.subagent_requests import CreateSubagentRequest, SubagentSnapshotRequest, subagent_environment
from app.agent.state.messages import BaseMessage
from app.agent.state.model import AgentState
from app.agent.tools.core.base import ToolContext
from app.agent.tools.core.runtime import ToolRuntime
from app.agent.tools.create_subagent import CreateSubagentTool
from tests.subagent_support import SubagentHarness


def test_cancelled_environment_wait_cancels_unneeded_snapshot_receipt() -> None:
    """取消模型前名单查询后，本次查询回执同步结束等待。"""
    async def scenario() -> None:
        """通过真实查询入口投递请求，在消费者返回结果前取消调用。"""
        queue: asyncio.Queue[RuntimeRequest] = asyncio.Queue()
        invocation = asyncio.create_task(subagent_environment(queue, "parent"))
        request = await asyncio.wait_for(queue.get(), 1)
        assert isinstance(request, SubagentSnapshotRequest)
        invocation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await invocation
        assert request.reply.cancelled()

    asyncio.run(scenario())


@pytest.mark.parametrize("valid_source", [True, False])
def test_scheduler_finishes_queued_request_after_caller_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, valid_source: bool,
) -> None:
    """回执等待取消后，Scheduler 仍能完成创建或拒绝过期调用，并继续处理队列。

    Args:
        tmp_path (Path): 本次测试独占的定义、历史和任务数据库目录。
        monkeypatch (pytest.MonkeyPatch): 观察已入队请求，保证取消与受理顺序确定。
        valid_source (bool): 创建请求是否属于父 Agent 当前执行。
    """
    async def scenario() -> None:
        """保持父子执行活动，取消第二次创建工具等待后检查真实持久结果。"""
        release = asyncio.Event()

        async def script(
            messages: Sequence[BaseMessage], tools: Sequence[ToolDefinition] | None,
        ) -> AsyncIterator[StreamChunk]:
            """让子任务保持活动，父模型不会因完成报告切换执行。

            Args:
                messages (Sequence[BaseMessage]): 子 Agent 本轮模型输入。
                tools (Sequence[ToolDefinition] | None): 子 Agent 本轮可用工具。

            Yields:
                StreamChunk: 测试结束时的子任务回答。
            """
            await release.wait()
            yield StreamChunk(content="完成")

        async with SubagentHarness(tmp_path / "state.sqlite3", script, parent_release=release) as harness:
            await harness.create()
            snapshot = await harness.scheduler.snapshot("parent")
            source_run = snapshot.payload["run_id"] if valid_source else "expired-run"
            runtime = ToolRuntime("parent", source_run, "create_subagent", "cancelled-call",
                                  harness.supervisor, harness.queue)
            submitted = asyncio.Event()
            requests: list[CreateSubagentRequest] = []
            put = harness.queue.put

            async def record_request(request: RuntimeRequest) -> None:
                """记录工具已交给共享队列的请求，再允许测试取消回执等待。

                Args:
                    request (RuntimeRequest): 本轮提交给实际 Scheduler 的请求。
                """
                await put(request)
                if isinstance(request, CreateSubagentRequest):
                    requests.append(request)
                    submitted.set()

            monkeypatch.setattr(harness.queue, "put", record_request)
            # 1. 暂缓消费者受理，确保工具已交出请求但仍在等待自己的回执。
            async with harness.scheduler._lifecycle_lock:
                invocation = asyncio.create_task(CreateSubagentTool({}).execute(
                    {"name": "第二个助手", "system_prompt": "完成任务", "prompt": "继续工作"},
                    ToolContext("parent", AgentState.create_new("parent"), runtime=runtime),
                ))
                await asyncio.wait_for(submitted.wait(), 1)
                invocation.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await invocation
                assert requests[0].reply.cancelled()
            # 2. 消费者独立完成原请求，随后还能返回正常名单查询结果。
            await asyncio.wait_for(harness.queue.join(), 3)
            summaries = await harness.scheduler.snapshot_subagents("parent")
            assert len(summaries) == (2 if valid_source else 1)
            assert any(item.name == "第二个助手" for item in summaries) == valid_source
            environment = await asyncio.wait_for(subagent_environment(harness.queue, "parent"), 1)
            assert (requests[0].name in environment) == valid_source
            release.set()

    asyncio.run(scenario())
