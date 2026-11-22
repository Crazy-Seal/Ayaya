"""验证应用装配在首轮对话前启动监听，并通过真实运行链路产生回复。"""

import asyncio
from pathlib import Path

import pytest
from fastapi import FastAPI

from app.agent.agent import Agent, AgentConfig
from app.agent.models.llm_client import StreamChunk
from app.agent.plugins.base import BasePlugin, PluginBootTask
from app.agent.plugins.registry import PluginRegistry
from app.agent.runtime.tasks.base import BaseTask
from app.agent.runtime.tasks.task_supervisor import TaskSupervisor
from app.crud.chat_settings_dao import ChatSettingsDao
from app.crud.sqlite_factory import SqliteFactory
from app.schemas.chat_settings import ChatSettings


def test_application_mounts_before_first_turn_and_closes_listeners(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """遗留整理后挂载全部启用会话，计时通知可经 Agent 和 Hub 产生回复。

    Args:
        tmp_path (Path): 本用例的隔离配置和数据库目录。
        monkeypatch (pytest.MonkeyPatch): 替代外部模型与应用的存储工厂。
    """
    import main
    from app.agent.runtime import runtime_factory

    async def scenario() -> None:
        """运行真实 lifespan，以可控等待替代随机时间流逝。"""
        release = asyncio.Event()
        order: list[str] = []
        agents: list[str] = []
        cleaned: list[str] = []

        class Listener(BaseTask):
            """等待计时信号后发送主动通知，继续监听到应用退出。"""

            async def run(self, runtime) -> None:
                """通过框架绑定的调用对象请求激活所属会话。

                Args:
                    runtime (PluginRuntime): 已绑定会话、插件和任务编号的调用对象。
                """
                self.session = runtime.session_id
                await release.wait()
                await runtime.notify(name="timer", text="主动聊聊天", activate_session=True)
                await asyncio.Event().wait()

            async def cleanup(self) -> None:
                """记录应用退出时完成的监听资源释放。"""
                cleaned.append(self.session)

        class Plugin(BasePlugin):
            """为每个启用的会话声明一个监听任务。"""

            name = "boot_lifespan_test"
            hooks = []

            @classmethod
            def on_application_boot_tasks(cls, *, context, config):
                """在遗留任务整理后生成当前会话的监听声明。

                Args:
                    context (PluginContext): 会话设置及数据库工厂。
                    config (dict): 已校验的插件参数副本。

                Returns:
                    tuple[PluginBootTask, ...]: 独立的监听任务。
                """
                order.append(context.chat_settings.session_id)
                return (PluginBootTask(Listener(), "timer", "随机聊天计时"),)

            async def execute(self, context) -> None:
                """本用例通过启动任务产生输入。

                Args:
                    context (HookContext): 普通 Agent 钩子上下文。
                """

        class Supervisor(TaskSupervisor):
            """观察真实遗留任务整理先于启动任务声明。"""

            async def recover_orphaned(self) -> None:
                """完成真实数据库整理，再记录启动顺序。"""
                await super().recover_orphaned()
                order.append("recovered")

        class Model:
            """替代外部模型，保留真实 Agent 输入交付和回复保存。"""

            async def astream(self, messages, tools=None):
                """生成最终回复，供全局观察连接接收。

                Args:
                    messages (Sequence[InputMessage]): 实际通知输入的模型投影。
                    tools (Sequence[ToolDefinition] | None): 本轮工具声明。

                Yields:
                    StreamChunk: 固定回答正文。
                """
                yield StreamChunk(content="来聊聊今天吧")

            async def close(self) -> None:
                """模型替身没有外部连接。"""

        def create_agent(settings, *args, session_state_manager, sqlite_factory, **kwargs):
            """只替代外部模型，保持真实 Agent 与应用借出的 Manager。

            Args:
                settings (ChatSettings): 当前会话的已准备配置。
                args (tuple): 本用例无需使用的外部工具装配材料。
                session_state_manager (SessionStateManager): 会话持有的真实状态对象。
                sqlite_factory (SqliteFactory): 本用例隔离存储。
                kwargs (dict): 其余工具与插件装配依赖。

            Returns:
                Agent: 使用固定模型回答的真实 Agent。
            """
            agents.append(settings.session_id)
            return Agent(AgentConfig(session_id=settings.session_id, model_name="test", api_key="test",
                    context_strategy=settings.context_strategy),
                llm_client=Model(), session_state_manager=session_state_manager, sqlite_factory=sqlite_factory)

        config_file = tmp_path / "settings.yaml"
        config_file.write_text("chat_models: []\n", encoding="utf-8")
        dao = ChatSettingsDao(config_file)
        for session in ("a", "b", "disabled"):
            dao.add_chat_settings(ChatSettings(session_id=session, model_name="test", openai_api_key="test",
                openai_base_url="http://localhost", temperature=0.7, system_prompt="test", context_strategy={},
                agent_plugins={Plugin.name: {"enabled": session != "disabled"}}))
        factory = SqliteFactory(tmp_path / "state.sqlite3")
        monkeypatch.setitem(PluginRegistry._plugins, Plugin.name, Plugin)
        monkeypatch.setattr(main, "ChatSettingsDao", lambda: dao)
        monkeypatch.setattr(main, "SqliteFactory", lambda: factory)
        monkeypatch.setattr(main, "TaskSupervisor", Supervisor)
        monkeypatch.setattr(runtime_factory, "build_agent", create_agent)
        application = FastAPI()
        async with main.lifespan(application):
            assert order == ["recovered", "a", "b"]
            assert agents == []
            scheduler = application.state.runtime_scheduler
            assert (await scheduler.snapshot("a")).payload["session_state"] == "paused"
            subscription = await application.state.interaction_event_hub.register()
            release.set()
            completed = set()
            async with asyncio.timeout(5):
                while len(completed) < 2:
                    event = await subscription.queue.get()
                    if event.kind == "run_completed":
                        completed.add(event.session_id)
            assert completed == {"a", "b"}
            assert sorted(agents) == ["a", "b"]
            summary = await main.active_task_summary(type("Request", (), {"app": application})())
            assert (summary.active_count, summary.application_boot_active_count) == (2, 2)
        assert sorted(cleaned) == ["a", "b"]
    asyncio.run(scenario())
