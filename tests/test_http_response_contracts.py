"""通过 HTTP 验证查询约束和对外响应契约。"""

from dataclasses import asdict
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.encoders import jsonable_encoder
from fastapi.testclient import TestClient

from app.dependencies import get_chat_history_service, get_mcp_service, get_runtime_scheduler, get_task_supervisor
from app.http_errors import install_http_error_handlers
from app.routes.agent import router
from app.routes.chat_history import router as history_router
from app.schemas.runtime_types import CommandReceipt, CommandStatus, TaskExecutionPhase, TaskStatus
from app.schemas.task_types import TaskRecord, TaskSnapshot


class _ForbiddenQueryService:
    """发现非法分页进入业务查询时立即失败。"""

    async def get_subagent_details(self, *_args: object) -> dict:
        """拒绝在参数校验失败时读取子 Agent。

        Args:
            *_args (object): 路由准备传入的子身份查询参数。

        Raises:
            AssertionError: 无效参数进入了业务查询。
        """
        raise AssertionError("非法查询参数不应进入业务方法")

    async def get_task_snapshot(self, *_args: object) -> TaskSnapshot:
        """拒绝在参数校验失败时读取任务。

        Args:
            *_args (object): 路由准备传入的任务查询参数。

        Raises:
            AssertionError: 无效参数进入了业务查询。
        """
        raise AssertionError("非法查询参数不应进入业务方法")


@pytest.mark.parametrize("path,params", [
    ("/agent/subagents/parent/child", {"interaction_limit": "0"}),
    ("/agent/subagents/parent/child", {"interaction_limit": "-1"}),
    ("/agent/subagents/parent/child", {"interaction_limit": "abc"}),
    ("/tasks/parent/task/output", {"offset": "-1"}),
    ("/tasks/parent/task/output", {"offset": "abc"}),
    ("/tasks/parent/task/output", {"limit": "0"}),
    ("/tasks/parent/task/output", {"limit": "-1"}),
    ("/tasks/parent/task/output", {"limit": "65537"}),
    ("/tasks/parent/task/output", {"limit": "abc"}),
])
def test_invalid_pagination_is_rejected_before_business_query(path: str, params: dict[str, str]) -> None:
    """非法分页返回 422，阻止子 Agent 和任务查询执行。

    Args:
        path (str): 需要分页校验的接口地址。
        params (dict[str, str]): 本次无效的查询参数。
    """
    application = FastAPI()
    application.include_router(router)
    service = _ForbiddenQueryService()
    application.dependency_overrides[get_runtime_scheduler] = lambda: service
    application.dependency_overrides[get_task_supervisor] = lambda: service
    with TestClient(application, raise_server_exceptions=False) as client:
        response = client.get(path, params=params)
    assert response.status_code == 422


@pytest.mark.parametrize("has_messages", [False, True])
def test_history_response_documents_and_preserves_message_fields(has_messages: bool) -> None:
    """历史接口公开消息结构，同时保留图片、来源、空值和空列表。

    Args:
        has_messages (bool): 当前会话是否已经保存了消息。
    """
    class HistoryService:
        """返回已经保存的用户消息。"""

        async def get_chat_history_data(self, **_kwargs: object) -> list[dict[str, object]]:
            """提供带图片与来源的固定历史。

            Args:
                **_kwargs (object): 当前会话和分页范围。

            Returns:
                list[dict[str, object]]: 本用例已经保存的可见消息。
            """
            if not has_messages:
                return []
            return [{"id": "message", "role": "Human", "content": "你好", "timestamp": "2026-11-21T10:00:00+00:00",
                     "images": None}]

    application = FastAPI()
    application.include_router(history_router)
    application.dependency_overrides[get_chat_history_service] = HistoryService
    with TestClient(application) as client:
        response = client.get("/chat_history/parent")
    expected = [{"id": "message", "role": "Human", "content": "你好",
        "timestamp": "2026-11-21T10:00:00+00:00", "images": None}] if has_messages else []
    assert response.json() == {"data": expected, "msg": "success", "code": 200}
    schema = application.openapi()
    reference = schema["paths"]["/chat_history/{session_id}"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
    result = schema["components"]["schemas"][reference.rsplit("/", 1)[-1]]
    item_reference = result["properties"]["data"]["items"]["$ref"]
    item_schema = schema["components"]["schemas"][item_reference.rsplit("/", 1)[-1]]
    assert {"id", "role", "content", "timestamp"} <= set(item_schema["required"])


@pytest.mark.parametrize("query,expected_limit", [(None, 10), ("1", 1), ("10000", 10000)])
def test_subagent_detail_preserves_retired_state_and_query_limit(query: str | None, expected_limit: int) -> None:
    """子详情接受合法数量并保留已销毁身份、时间和历史结果。

    Args:
        query (str | None): 省略或显式指定的交互数量。
        expected_limit (int): 应交给查询服务的数量。
    """
    calls = []
    record = TaskRecord("run", "parent", None, TaskStatus.COMPLETED, None, None, None,
                        "2026-11-21T01:00:00+00:00", "2026-11-21T02:00:00+00:00", subagent_id="child")
    detail = {"subagent_id": "child", "name": "助手", "system_prompt": "分析文件", "if_retain_context": False,
              "created_at": "2026-11-21T00:00:00+00:00", "tool_whitelist": ["local:read_file"],
              "context_available": False, "status": None, "results": [asdict(record)],
              "recent_interactions": [{"interaction_id": 1, "subagent_id": "child", "kind": "final", "text": "已完成",
                  "created_at": "2026-11-21T02:00:00+00:00", "source_id": "run"}]}

    class Scheduler:
        """提供已销毁子身份的保存记录。"""

        async def get_subagent_details(self, session_id: str, subagent_id: str, limit: int) -> dict:
            """记录路由实际使用的查询范围并返回详情。

            Args:
                session_id (str): 父会话编号。
                subagent_id (str): 需要查询的子身份。
                limit (int): 最多返回的交互数量。

            Returns:
                dict: 已销毁子身份的完整历史详情。
            """
            calls.append((session_id, subagent_id, limit))
            return detail

    application = FastAPI()
    application.include_router(router)
    application.dependency_overrides[get_runtime_scheduler] = Scheduler
    with TestClient(application) as client:
        response = client.get("/agent/subagents/parent/child", params={} if query is None else {"interaction_limit": query})
    assert response.status_code == 200
    assert response.json() == jsonable_encoder(detail)
    assert calls == [("parent", "child", expected_limit)]


@pytest.mark.parametrize("params,expected_offset,expected_size", [
    ({}, 16384, 16384),
    ({"offset": 0, "limit": 1}, 1, 1),
    ({"offset": 0, "limit": 65536}, 65536, 65536),
    ({"offset": 70000, "limit": 1}, 70000, 0),
])
def test_task_output_accepts_defaults_and_inclusive_bounds(
    tmp_path: Path, params: dict[str, int], expected_offset: int, expected_size: int,
) -> None:
    """输出分页保留默认预算、合法边界及文件末尾行为。

    Args:
        tmp_path (Path): 当前用例独立的输出文件目录。
        params (dict[str, int]): 本次合法分页参数。
        expected_offset (int): 下一页应使用的字节游标。
        expected_size (int): 当前页应交付的文本长度。
    """
    output = tmp_path / "terminal.txt"
    output.write_bytes(b"a" * 70000)
    record = TaskSnapshot("task", "parent", "echo", TaskStatus.COMPLETED, 1, 0, str(output), "start", "end")

    class Supervisor:
        """为输出接口提供已结束的终端快照。"""

        async def get_task_snapshot(self, _task_id: str) -> TaskSnapshot:
            """返回当前用例独立输出文件的任务。

            Args:
                _task_id (str): 需要查询的任务编号。

            Returns:
                TaskSnapshot: 关联独立输出文件的已结束任务。
            """
            return record

    application = FastAPI()
    application.include_router(router)
    application.dependency_overrides[get_task_supervisor] = Supervisor
    with TestClient(application) as client:
        response = client.get("/tasks/parent/task/output", params=params)
    assert response.status_code == 200
    assert response.json() == {"text": "a" * expected_size, "next_offset": expected_offset, "has_more": expected_offset < 70000}


@pytest.mark.parametrize("has_output", [False, True])
def test_task_list_preserves_full_snapshot_including_save_failure(tmp_path: Path, has_output: bool) -> None:
    """任务列表完整交付保存失败快照、来源、通知和输出字段。

    Args:
        tmp_path (Path): 当前用例独立的输出目录。
        has_output (bool): 当前任务是否关联终端输出。
    """
    output = tmp_path / "output.txt"
    output.write_text("执行完成", encoding="utf-8")
    record = TaskSnapshot("task", "parent", None, TaskExecutionPhase.SAVE_FAILED, None, 0, str(output) if has_output else None,
        "2026-11-21T01:00:00+00:00", None, origin_run_id="run", origin_tool_call_id="call", output_ref="ref",
        background=False, tool_name=None, name="work", title="保存结果", result="业务已经完成", error="保存失败",
        plugin_name="example", notify_agent=False, subagent_id="child", notification_context_id="parent",
        end_reason="completed", phase=TaskExecutionPhase.SAVE_FAILED, save_error="磁盘无法写入")

    class Supervisor:
        """提供仍保留在进程中的保存失败快照。"""

        async def list_tasks(self, _session_id: str) -> list[TaskSnapshot]:
            """返回当前任务的完整查询字段。

            Args:
                _session_id (str): 任务所属的会话编号。

            Returns:
                list[TaskSnapshot]: 含保存失败资料的完整快照。
            """
            return [record]

    application = FastAPI()
    application.include_router(router)
    application.dependency_overrides[get_task_supervisor] = Supervisor
    with TestClient(application) as client:
        response = client.get("/tasks/parent/records")
    assert response.status_code == 200
    assert response.json() == {"tasks": [{**jsonable_encoder(asdict(record)), "recent_output": "执行完成" if has_output else ""}]}


@pytest.mark.parametrize("status,accepted", [(CommandStatus.ACCEPTED, True), (CommandStatus.INVALID, False)])
def test_command_receipt_keeps_computed_acceptance_and_null_fields(status: CommandStatus, accepted: bool) -> None:
    """命令接口保留受理标记和空值，未知类别继续交给业务处理。

    Args:
        status (CommandStatus): 本次业务受理结果。
        accepted (bool): 对外应报告的受理标记。
    """
    calls = []

    class Scheduler:
        """按当前用例返回业务回执。"""

        async def submit(self, command) -> CommandReceipt:
            """接收未知类别并记录原始动态载荷。

            Args:
                command (InteractionCommand): 路由从请求构造的业务命令。

            Returns:
                CommandReceipt: 当前用例指定的业务受理结果。
            """
            calls.append((command.kind, command.payload))
            return CommandReceipt(status)

    application = FastAPI()
    application.include_router(router)
    application.dependency_overrides[get_runtime_scheduler] = Scheduler
    with TestClient(application) as client:
        response = client.post("/agent/commands", json={"session_id": "parent", "kind": "unknown", "payload": {"nested": [None, {"flag": True}]}})
    assert response.status_code == 200
    assert response.json() == {"status": status.value, "accepted": accepted, "context_id": None, "run_id": None, "detail": None}
    assert calls == [("unknown", {"nested": [None, {"flag": True}]})]


@pytest.mark.parametrize("method,path,payload", [
    ("GET", "/agent/subagents/parent/child", None),
    ("POST", "/mcp/servers", {"id": "demo", "name": "Demo", "transport": "stdio", "command": "python"}),
])
def test_missing_required_response_field_fails_http_validation(method: str, path: str, payload: dict | None) -> None:
    """真实路由发现业务结果缺少必需字段时返回服务端错误。

    Args:
        method (str): 本次查询或创建操作的 HTTP 方法。
        path (str): 使用具体响应模型的接口地址。
        payload (dict | None): 合法的创建请求，查询时为空。
    """
    from app.routes.mcp import router as mcp_router

    class BrokenService:
        """模拟业务返回了不完整的输出数据。"""

        async def get_subagent_details(self, *_args: object) -> dict:
            """遗漏必须交付的子身份字段。

            Args:
                *_args (object): 当前会话、子身份和分页数量。

            Returns:
                dict: 缺少必需响应字段的错误业务结果。
            """
            return {}

        async def create_server(self, *_args: object) -> dict:
            """遗漏必须交付的服务器视图字段。

            Args:
                *_args (object): 已通过请求校验的服务器配置。

            Returns:
                dict: 缺少必需响应字段的错误业务结果。
            """
            return {}

    application = FastAPI()
    application.include_router(router)
    application.include_router(mcp_router)
    install_http_error_handlers(application)
    application.dependency_overrides[get_runtime_scheduler] = BrokenService
    application.dependency_overrides[get_mcp_service] = BrokenService
    with TestClient(application, raise_server_exceptions=False) as client:
        response = client.request(method, path, json=payload)
    assert response.status_code == 500


def test_openapi_exposes_pagination_and_concrete_json_responses() -> None:
    """所有普通接口公开固定响应字段，分页文档与接受范围一致。"""
    from main import app

    schema = app.openapi()
    schemas = schema["components"]["schemas"]
    for path, methods in schema["paths"].items():
        for operation in methods.values():
            content = operation["responses"]["200"].get("content", {})
            if path == "/agent/events":
                continue
            response_schema = content["application/json"]["schema"]
            response_schema = schemas[response_schema["$ref"].rsplit("/", 1)[-1]]
            assert response_schema["type"] == "object"
            assert response_schema["properties"]
            if "data" in response_schema["properties"]:
                data = response_schema["properties"]["data"]
                assert "$ref" in data or data.get("type") in {"array", "null"}
    detail = schema["paths"]["/agent/subagents/{session_id}/{subagent_id}"]["get"]["parameters"]
    limit = next(item["schema"] for item in detail if item["name"] == "interaction_limit")
    assert (limit["default"], limit["minimum"]) == (10, 1)
    output = schema["paths"]["/tasks/{session_id}/{task_id}/output"]["get"]["parameters"]
    offset = next(item["schema"] for item in output if item["name"] == "offset")
    budget = next(item["schema"] for item in output if item["name"] == "limit")
    assert (offset["default"], offset["minimum"]) == (0, 0)
    assert (budget["default"], budget["minimum"], budget["maximum"]) == (16384, 1, 65536)


def test_process_control_responses_preserve_wire_format() -> None:
    """进程查询和关闭接口保留 Electron 使用的响应字段。"""
    import os
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    import main

    application = FastAPI()
    application.add_api_route("/", main.root)
    application.add_api_route("/internal/ready", main.internal_ready)
    application.add_api_route("/internal/tasks/active-summary", main.active_task_summary)
    application.add_api_route("/internal/shutdown", main.internal_shutdown, methods=["POST"])
    application.state.runtime_scheduler = SimpleNamespace(begin_shutdown=AsyncMock())
    application.state.plugin_boot_task_manager = SimpleNamespace(close=AsyncMock())
    application.state.interaction_event_hub = SimpleNamespace(begin_shutdown=Mock())
    application.state.task_supervisor = SimpleNamespace(begin_shutdown=AsyncMock(), active_task_counts=lambda: (2, 1))
    with TestClient(application) as client:
        assert client.get("/").json() == {"message": "Ayaya server is running"}
        assert client.get("/internal/ready").json() == {"ready": True, "pid": os.getpid()}
        assert client.get("/internal/tasks/active-summary").json() == {"active_count": 2, "application_boot_active_count": 1}
        assert client.post("/internal/shutdown").status_code == 503
        server = SimpleNamespace(should_exit=False)
        application.state.uvicorn_server = server
        response = client.post("/internal/shutdown")
    assert response.status_code == 200
    assert response.json() == {"shutting_down": True}
    assert server.should_exit is True


def test_settings_crud_preserves_dynamic_configuration_and_delete_null() -> None:
    """设置增查改返回完整配置，删除仍交付 data 为 null 的成功响应。"""
    from unittest.mock import Mock

    from app.dependencies import get_chat_settings_service
    from app.routes.chat_settings import router as settings_router
    from app.schemas.chat_settings import ChatSettings

    settings = ChatSettings(session_id="parent", model_name="model", openai_api_key="test",
        openai_base_url="http://127.0.0.1:1/v1", temperature=0.1, system_prompt="角色", context_strategy={},
        agent_plugins={"example": {"enabled": False, "config": {"nested": [None, {"enabled": True, "value": 3.5}]}}})

    class SettingsService:
        """为接口返回包含动态插件配置的完整会话设置。"""

        async def add_chat_settings(self, incoming: ChatSettings) -> ChatSettings:
            """返回本次创建请求的完整配置。

            Args:
                incoming (ChatSettings): 用户提交的创建配置。

            Returns:
                ChatSettings: 本次创建成功后的配置。
            """
            return incoming

        def get_chat_settings_by_session(self, _session_id: str) -> ChatSettings:
            """读取当前用例准备的配置。

            Args:
                _session_id (str): 需要读取的会话编号。

            Returns:
                ChatSettings: 带动态插件参数的完整配置。
            """
            return settings

        async def update_chat_settings(self, incoming: ChatSettings) -> ChatSettings:
            """返回本次更新请求的完整配置。

            Args:
                incoming (ChatSettings): 用户提交的更新配置。

            Returns:
                ChatSettings: 本次更新成功后的配置。
            """
            return incoming

        async def delete_chat_settings(self, _session_id: str) -> None:
            """完成配置删除的业务入口。

            Args:
                _session_id (str): 需要删除的会话编号。
            """

    application = FastAPI()
    application.include_router(settings_router)
    application.dependency_overrides[get_chat_settings_service] = SettingsService
    scheduler = Mock()
    application.dependency_overrides[get_runtime_scheduler] = lambda: scheduler
    payload = settings.model_dump(mode="json")
    with TestClient(application) as client:
        responses = [client.post("/chat_settings", json=payload), client.get("/chat_settings/parent"),
                     client.put("/chat_settings", json=payload)]
        deleted = client.delete("/chat_settings/parent")
    for response in responses:
        assert response.status_code == 200
        assert response.json() == {"data": payload, "msg": "success", "code": 200}
    assert deleted.status_code == 200
    assert deleted.json() == {"data": None, "msg": "success", "code": 200}
