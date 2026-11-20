"""通过原生工具公开入口验证执行参数和结果转换。"""

import asyncio

from app.agent.state.model import AgentState
from app.agent.state.messages import ImageContent
from app.agent.tools.core.base import ToolContext
from app.agent.tools.python import PythonTool
from app.agent.tools.python_reset import PythonResetTool
from app.repl.repl_service import ReplService
from app.schemas.message_types import MediaKind


def test_python_tool_returns_ordered_tool_image_and_reset_clears_state(tmp_path) -> None:
    """原生工具沿用普通工具图片，reset 后变量不可再用。

    Args:
        tmp_path (Path): 本次 runner 的独立工作目录。
    """
    async def scenario() -> None:
        """调用工具公开 execute 并核验模型将收到的内容块。"""
        service = ReplService(workdir=tmp_path)
        tool = PythonTool(service, ())
        reset = PythonResetTool(service)
        context = ToolContext("s", AgentState(session_id="s"), origin_run_id="run", origin_tool_call_id="call")
        try:
            result = await tool.execute({"intent": "生成图像", "code":
                "from PIL import Image\nx = 42\nprint('before')\ndisplay_image(Image.new('RGB', (2, 2)))\nprint('after')"}, context)
            assert result.content[0].text == "before\n"
            assert result.content[2].text == "after\n"
            picture = next(item for item in result.content if isinstance(item, ImageContent))
            assert picture.media_kind == MediaKind.TOOL
            assert picture.url.startswith("data:image/png;base64,")
            assert result.prepared_action is None
            await reset.execute({}, context)
            cleared = await tool.execute({"intent": "检查重置", "code": "print('x' in globals())"}, context)
            assert cleared.text == "False\n"
            invalid = await tool.execute({"code": "print('不应运行')"}, context)
            assert "intent" in invalid.text
        finally:
            await service.close()
    asyncio.run(scenario())


def test_approval_rejects_changed_intent_or_code(repl_service) -> None:
    """一次批准同时绑定目的和代码，任一变化都要求重新审批。

    Args:
        repl_service (ReplService): 尚未执行代码的测试资源。
    """
    from dataclasses import replace
    from app.agent.tools.authorization.manager import ToolAuthorization, AuthorizationRequired, ToolInvocation
    from app.agent.tools.core.base import ToolResult
    from tests.test_agent_factory import _settings
    from tests.test_tool_authorization import _descriptor

    settings = _settings(agent_plugins={})
    authorization = ToolAuthorization(lambda _: settings)
    descriptor = _descriptor("python")
    original = ToolInvocation(settings.session_id, "call", descriptor, {"intent": "计算", "code": "print(1)"})
    pending = authorization.authorize(original)
    assert isinstance(pending, AuthorizationRequired)
    assert pending.interrupt.data["arguments"] == original.arguments
    assert pending.interrupt.data["prepared_action"] is None
    for arguments in ({"intent": "新目的", "code": "print(1)"}, {"intent": "计算", "code": "print(2)"}):
        changed = replace(original, arguments=arguments)
        result = authorization.resolve_authorization(changed, decision={"kind": "approve"},
                                                      authorization_snapshot=pending.resume_state)
        assert isinstance(result, ToolResult) and "重新发起" in result.text


def test_factory_installs_native_tools_and_child_grant_fails(tmp_path) -> None:
    """真实工厂预装工具，自动扫描可完成，创建子 Agent 时拒绝授予 REPL。

    Args:
        tmp_path (Path): runner 和 SQLite 的独立目录。
    """
    from app.agent.runtime.agent_factory import build_agent
    from app.agent.tools.authorization.manager import ToolAuthorization
    from app.agent.tools.core.runtime import ToolRuntime
    from app.agent.runtime.tasks.task_supervisor import TaskSupervisor
    from app.crud.task_record_dao import TaskRecordDao
    from app.crud.sqlite_factory import SqliteFactory
    from tests.test_agent_factory import _settings
    from tests.test_repl_service import capability_context
    import pytest

    async def scenario() -> None:
        """从真实模型目录取工具，验证共享资源和白名单拒绝。
        """
        service = ReplService(workdir=tmp_path)
        sqlite = SqliteFactory(tmp_path / "state.sqlite3")
        settings = _settings(agent_plugins={})
        queue = asyncio.Queue()
        supervisor = TaskSupervisor(runtime_queue=queue, records=TaskRecordDao(sqlite))
        runtime = ToolRuntime(settings.session_id, "run", "create_subagent", "call", _supervisor=supervisor, _runtime_queue=queue)
        agent = build_agent(settings, tool_authorization=ToolAuthorization(lambda _: settings),
                            tool_runtime_factory=lambda *args: runtime, sqlite_factory=sqlite,
                            repl_service=service, repl_capabilities=capability_context().capabilities)
        try:
            await agent.initialize()
            tool = agent.tool_manager.get("python")
            assert tool is not None and agent.tool_manager.get("python_reset") is not None
            context = ToolContext(settings.session_id, AgentState(session_id=settings.session_id), runtime=runtime)
            result = await tool.execute({"intent": "验证预注入", "code": "sample.inspect()"}, context)
            assert result.text == "12 False\n"
            create = agent.tool_manager.get("create_subagent")
            assert create is not None
            for name in ("python", "python_reset"):
                with pytest.raises(ValueError, match="当前可委派目录"):
                    await create.execute({"name": "child", "system_prompt": "test", "prompt": "test", "tool_whitelist": [name]}, context)
            assert queue.empty()
        finally:
            await agent.close()
            await service.close()
            await supervisor.close()
            await supervisor.records.close()
    asyncio.run(scenario())
