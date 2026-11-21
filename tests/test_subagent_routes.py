"""通过 HTTP 验证子身份查询归属及统一命令业务拒绝。"""
from tests.runtime_support import runtime_hub, close_test_runtime
from app.routes.agent import observe_runtime
from tests.runtime_support import build_test_scheduler
from app.crud.sqlite_factory import SqliteFactory

import asyncio
from contextlib import asynccontextmanager
from tests.runtime_support import configured_settings
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agent.runtime.agent_runtime_scheduler import AgentRuntimeScheduler
from app.agent.state.manager import SessionStateManager
from app.crud.subagent_dao import SubagentDao
from app.dependencies import get_runtime_scheduler
from app.http_errors import install_http_error_handlers
from app.routes.agent import router
from app.schemas.subagent_types import SubagentDefinition


def test_subagent_queries_and_invalid_controls_preserve_http_business_contract(tmp_path: Path) -> None:
    """列表及详情不执行模型，跨父详情返回 404，无效控制返回受理拒绝而不是服务端错误。"""
    path = str(tmp_path / "state.sqlite3")

    @asynccontextmanager
    async def lifespan(application):
        """在 TestClient 同一事件循环准备重启后的空闲子身份并负责清理。"""
        dao = SubagentDao(SqliteFactory(path))
        definition = SubagentDefinition("child", "parent", "代码探索助手", "检查项目", frozenset(), True, datetime.now(timezone.utc))
        await dao.create_with_command(definition, "首次工作", "create")
        await dao.close()

        def forbidden(*_args):
            """纯查询与无效控制不应启动模型。"""
            raise AssertionError("查询不应执行 Agent")

        scheduler = build_test_scheduler(chat_settings_dao=configured_settings(), runtime_queue=asyncio.Queue(), agent_factory=forbidden,
            subagent_factory=forbidden, session_state_manager_factory=lambda sid: SessionStateManager(sid, SqliteFactory(path)))
        application.state.scheduler = scheduler
        await scheduler.start()
        try:
            yield
        finally:
            await close_test_runtime(scheduler)

    application = FastAPI(lifespan=lifespan)
    install_http_error_handlers(application)
    application.include_router(router)
    application.dependency_overrides[get_runtime_scheduler] = lambda: application.state.scheduler
    with TestClient(application) as client:
        response = client.get("/agent/subagents/parent")
        assert response.status_code == 200
        assert [(item["subagent_id"], item["status"]) for item in response.json()["subagents"]] == [("child", "idle")]
        detail = client.get("/agent/subagents/parent/child")
        assert detail.json()["system_prompt"] == "检查项目"
        assert client.get("/agent/subagents/another-parent/child").status_code == 404
        for subagent_id, payload in [("unknown", {"action": "stop"}), ("child", {"action": "command", "text": ""}),
                                      ("child", {"action": "invalid"})]:
            rejected = client.post("/agent/commands", json={"session_id": "parent", "origin_channel": "desktop",
                "kind": "interact_subagent", "subagent_id": subagent_id, "payload": payload})
            assert rejected.status_code == 200
            assert rejected.json()["accepted"] is False
