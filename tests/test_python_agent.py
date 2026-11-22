"""用真实 Agent、审批、停止和图片保存链路验证原生 Python 工具。"""

import asyncio

import pytest

from app.agent.agent import Agent, AgentConfig
from app.agent.context import ContextStrategyConfig
from app.agent.models.contracts import StreamChunk
from app.agent.state.messages import ImageContent, ToolCall, ToolMessage
from app.agent.tools.authorization.manager import ToolAuthorization
from app.agent.tools.python import PythonTool
from app.crud.sqlite_factory import SqliteFactory
from app.environment import get_images_dir
from app.repl.contracts import ReplCallContext, ReplEndReason
from app.repl.repl_service import ReplService
from app.schemas.chat_settings import ChatSettings
from app.schemas.message_types import MediaKind
from app.schemas.runtime_types import AgentRunOutcomeKind
from tests.runtime_support import make_execution_control
from tests.test_checkpoint_recovery import ScriptedLLM
from tests.test_repl_service import capability_context, wait_for_file


@pytest.mark.parametrize("policy", ["allow", "ask"])
@pytest.mark.parametrize("action", ["stop", "steer", "steer_then_stop"])
def test_real_agent_control_preserves_runner_and_waits_for_cleanup(tmp_path, policy, action) -> None:
    """直接执行或审批恢复后，停止保留 REPL，引导等待真实结果。

    Args:
        tmp_path (Path): SQLite、runner 和执行屏障的独立目录。
        policy (str): 当前工具直接允许或需要审批。
        action (str): 请求停止、引导或引导后停止。
    """
    async def scenario() -> None:
        """在真实代码到达屏障后发出控制，读取实际保存结果。"""
        service = ReplService(workdir=tmp_path)
        capabilities = capability_context().capabilities
        code = ("from pathlib import Path\nimport os, time\nsaved = 42\n"
                "Path('pid').write_text(str(os.getpid()))\nsample.cleanup_file('cleaned')\n"
                "Path('running').touch()\nwhile not Path('release').exists(): time.sleep(0.01)\nprint(saved)")
        args = {"intent": "测试持久执行", "code": code}
        settings = ChatSettings(session_id="s", model_name="test", openai_api_key="test",
            openai_base_url="http://unused", temperature=0, system_prompt="test", context_strategy={},
            agent_plugins={}, tool_permissions={"local:python": policy})
        agent = Agent(AgentConfig("s", "test", "test", ContextStrategyConfig(compression_trigger_tokens_k=100)),
                      SqliteFactory(tmp_path / "state.sqlite3"),
                      llm_client=ScriptedLLM([[StreamChunk(tool_call=ToolCall("python-call", "python", args))]]),
                      tool_authorization=ToolAuthorization(lambda _: settings), enable_plugin_hooks=False)
        agent.register_tool(PythonTool(service, capabilities))
        machine, control = make_execution_control("run")
        events = []

        async def receive(event) -> None:
            """保存 Agent 的公开执行和审批事件。

            Args:
                event (AgentEvent): 供界面观察的正式事件。
            """
            events.append(event)

        invocation = None
        try:
            if policy == "ask":
                assert (await agent.run("执行", turn_control=control, emit=receive)).kind == AgentRunOutcomeKind.INTERRUPTED
                assert not (tmp_path / "running").exists()
                approval = events[-1].data
                assert approval["data"]["arguments"] == args
                request_id = approval["request_id"]
                await agent.session_state_manager.record_authorization_decision(request_id, {"kind": "approve"})
                assert machine.resume_approval("run", request_id).accepted
                invocation = asyncio.create_task(agent.resume(request_id, turn_control=control, emit=receive))
            else:
                invocation = asyncio.create_task(agent.run("执行", turn_control=control, emit=receive))
            await wait_for_file(tmp_path / "running")
            if action in {"steer", "steer_then_stop"}:
                assert machine.request_steer(expected_run_id="run").accepted
                control.wake_interrupt()
                assert not invocation.done()
            if action != "steer":
                assert machine.request_stop("run").accepted
                control.wake_interrupt()
            else:
                (tmp_path / "release").touch()
            outcome = await asyncio.wait_for(invocation, 5)
            assert outcome.kind == (AgentRunOutcomeKind.STEERED if action == "steer" else AgentRunOutcomeKind.STOPPED)
            assert (tmp_path / "cleaned").exists()
            state = await agent.session_state_manager.load()
            results = [item for item in state.messages if isinstance(item, ToolMessage)]
            assert len(results) == 1
            assert results[0].text == ("42\n" if action == "steer" else "被用户取消，工具调用未完成")
            await agent.close()
            after = await service.execute("print(saved)", ReplCallContext("s", "next-run", "next-call", capabilities))
            assert after.reason == ReplEndReason.COMPLETED
            assert after.pid == int((tmp_path / "pid").read_text())
            assert "".join(item.text for item in after.outputs) == "42\n"
        finally:
            if invocation is not None:
                invocation.cancel()
                await asyncio.gather(invocation, return_exceptions=True)
            await agent.close()
            await service.close()
    asyncio.run(scenario())


def test_python_image_uses_existing_tool_result_persistence(tmp_path) -> None:
    """Agent 保存原生 Python 图片后，历史中的工具图片带有可读取文件引用。

    Args:
        tmp_path (Path): 隔离进程目录和数据库。
    """
    async def scenario() -> None:
        """完成真实工具轮次，通过状态读取已保存图片。"""
        service = ReplService(workdir=tmp_path)
        call = ToolCall("image-call", "python", {"intent": "生成测试图", "code":
            "from PIL import Image\ndisplay_image(Image.new('RGB', (3, 2), 'blue'))"})
        agent = Agent(AgentConfig("s", "test", "test", ContextStrategyConfig(compression_trigger_tokens_k=100)),
            SqliteFactory(tmp_path / "state.sqlite3"),
            llm_client=ScriptedLLM([[StreamChunk(tool_call=call)], [StreamChunk(content="完成")]]),
            enable_plugin_hooks=False)
        agent.register_tool(PythonTool(service, ()))
        _, control = make_execution_control("run")
        try:
            await agent.run("生成", turn_control=control, emit=lambda event: asyncio.sleep(0))
            state = await agent.session_state_manager.load()
            image = next(part for message in state.messages if isinstance(message, ToolMessage)
                         for part in message.content if isinstance(part, ImageContent))
            assert image.media_kind == MediaKind.TOOL and image.storage_ref
            assert (get_images_dir() / image.storage_ref).is_file()
        finally:
            await agent.close()
            await service.close()
    asyncio.run(scenario())
