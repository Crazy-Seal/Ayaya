"""使用真实 Scheduler、Agent 和 SQLite 的子功能测试装配，只替换模型与父发起动作。"""
from tests.runtime_support import RuntimeAgentStub

import asyncio
from collections.abc import AsyncIterator, Callable, Sequence
from tests.runtime_support import configured_settings
from pathlib import Path

from app.agent.agent import Agent, AgentConfig
from app.agent.context import ContextStrategyConfig
from app.agent.core.events import AgentEvent, EventType
from app.agent.models.contracts import StreamChunk, ToolDefinition
from app.agent.runtime.agent_runtime_scheduler import AgentRuntimeScheduler
from app.agent.runtime.subagent_requests import CreateSubagentRequest, SubagentSource
from app.agent.runtime.tasks.task_supervisor import TaskSupervisor
from app.agent.state.manager import SessionStateManager
from app.agent.state.messages import BaseMessage
from app.agent.tools.core.base import BaseTool
from app.agent.tools.core.runtime import ToolRuntime
from app.agent.tools.authorization.manager import ToolAuthorization
from app.agent.tools._subagent_support import MessageParentTool
from app.interaction.interaction_contract import ChannelCommand
from app.schemas.tool_identity import ToolIdentity
from app.schemas.subagent_types import SubagentPublicStatus

ModelScript = Callable[[Sequence[BaseMessage], Sequence[ToolDefinition] | None], AsyncIterator[StreamChunk]]


async def wait_for_subagent_worker(scheduler: AgentRuntimeScheduler, subagent_id: str) -> None:
    """测试等待已登记的子 worker 完成，随后通过公开查询核对报告和资源结果。

    Args:
        scheduler (AgentRuntimeScheduler): 测试实际运行的调度器。
        subagent_id (str): 本次需要等待的执行上下文。
    """
    worker = scheduler._session(subagent_id).worker
    if worker is not None:
        await asyncio.shield(worker)


def subagent_worker_running(scheduler: AgentRuntimeScheduler, subagent_id: str) -> bool:
    """供清理屏障测试核对原 worker 是否仍持有执行和资源。

    Args:
        scheduler (AgentRuntimeScheduler): 测试实际运行的调度器。
        subagent_id (str): 本次需要核对的执行上下文。

    Returns:
        bool: 已登记且仍未结束的 worker 返回 True。
    """
    worker = scheduler._session(subagent_id).worker
    return worker is not None and not worker.done()


async def wait_for_subagent_status(scheduler: AgentRuntimeScheduler, subagent_id: str,
                                   status: SubagentPublicStatus) -> None:
    """测试从父会话公开名单等待子执行进入指定状态。

    Args:
        scheduler (AgentRuntimeScheduler): 测试实际运行的调度器。
        subagent_id (str): 需要观察的子身份。
        status (SubagentPublicStatus): 预期的公开运行状态。
    """
    while True:
        summaries = await scheduler.snapshot_subagents("parent")
        if any(item.subagent_id == subagent_id and item.status == status for item in summaries):
            return
        await asyncio.sleep(0.01)


class SubagentHarness:
    """为测试保存隔离实例及模型脚本，所有行为通过公开命令和查询观察。"""

    def __init__(self, path: Path, script: ModelScript, *, retain: bool = True,
                 tools: tuple[BaseTool, ...] = (), authorization: ToolAuthorization | None = None,
                 parent_release: asyncio.Event | None = None) -> None:
        """指定数据库、模型脚本及额外任务工具。"""
        self.path, self.script, self.retain, self.tools = str(path), script, retain, tools
        self.authorization, self.parent_release = authorization, parent_release
        self.queue = asyncio.Queue()
        self.created = asyncio.Queue()
        self.received = asyncio.Queue()
        self.supervisor = TaskSupervisor(chat_settings_dao=configured_settings(), runtime_queue=self.queue, db_path=self.path)
        harness = self

        class Parent(RuntimeAgentStub):
            """父侧只负责通过共享队列发起创建和接收真实 Inbox 内容。"""
            def __init__(self, manager: SessionStateManager) -> None:
                """借用父 worker 的 Manager。"""
                self.manager = manager

            async def run(self, message, *, turn_control, inbox_inputs, **kwargs):
                """保存真人发起动作并创建一个子身份。"""
                yield AgentEvent(EventType.RUN_STARTED, {"origin_channel": "desktop", "resumed": False})
                state = await self.manager.load()
                await self.manager.commit_human_input(state, message, None, inputs=inbox_inputs)
                source = SubagentSource("parent", turn_control.run_id, "create")
                future = asyncio.get_running_loop().create_future()
                identities = frozenset(getattr(tool, "identity", None) or ToolIdentity.local(tool.name) for tool in harness.tools)
                await harness.queue.put(CreateSubagentRequest(source, "测试助手", "完成交代的任务", message,
                                                                identities, harness.retain, future))
                await harness.created.put(await future)
                if harness.parent_release is not None:
                    await harness.parent_release.wait()
                yield AgentEvent(EventType.DONE, None)

            async def run_runtime_inputs(self, *, inbox_inputs, **kwargs):
                """通过实际 Manager 消费后台报告并通知测试观察者。"""
                yield AgentEvent(EventType.RUN_STARTED, {"origin_channel": "desktop", "resumed": False})
                state = await self.manager.load()
                await self.manager.commit_runtime_inputs(state, None, inbox_inputs)
                await harness.received.put(inbox_inputs)
                yield AgentEvent(EventType.DONE, None)

            async def close(self, *, raise_on_error: bool = False) -> None:
                """Manager 生命周期属于 worker。"""

        self.scheduler = AgentRuntimeScheduler(runtime_queue=self.queue,
            agent_factory=lambda _session, manager: Parent(manager), subagent_factory=self._child,
            task_supervisor=self.supervisor,
            session_state_manager_factory=lambda context: SessionStateManager(context, self.path))

    def _child(self, definition, manager: SessionStateManager) -> Agent:
        """真实子 Agent 使用已确认的配置及资源所有权，只替换网络模型。"""
        script = self.script

        class Model:
            """将固定脚本作为真实管道的流式模型。"""
            async def astream(self, messages, *, tools=None):
                """依次生成脚本返回的模型分片或工具调用。"""
                async for chunk in script(messages, tools):
                    yield chunk

            async def close(self) -> None:
                """脚本不持有网络连接。"""

        agent = Agent(AgentConfig(session_id=definition.subagent_id, model_name="test", api_key="test",
                                 system_prompt=definition.system_prompt,
                                 context_strategy=ContextStrategyConfig(compression_trigger_tokens_k=100)),
            llm_client=Model(), session_state_manager=manager, enable_plugin_hooks=False,
            permission_session_id=definition.session_id, tool_whitelist=definition.tool_whitelist,
            tool_authorization=self.authorization,
            tool_runtime_factory=lambda name, run, call: ToolRuntime(definition.session_id, run, name, call,
                self.supervisor, self.queue, subagent_id=definition.subagent_id))
        agent.tool_manager.register_framework_tool(MessageParentTool())
        for tool in self.tools:
            agent.register_tool(tool)
        return agent

    async def __aenter__(self):
        """启动共享消费者，测试之间不复用应用数据。"""
        await self.scheduler.start()
        return self

    async def create(self, text: str = "第一项任务"):
        """通过父公开命令开始创建，取得受理回执。"""
        result = await self.scheduler.submit(ChannelCommand("parent", "desktop", "human_turn", {"message": text}))
        assert result.accepted
        return await asyncio.wait_for(self.created.get(), 3)

    async def __aexit__(self, *_args) -> None:
        """先结束 Agent 生产者，再关闭普通后台任务，避免遗留子进程。"""
        await self.scheduler.close()
        await self.supervisor.close()
