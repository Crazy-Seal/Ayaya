from app.crud.sqlite_factory import SqliteFactory
from app.agent.state.messages import ImageContent
from tests.model_capture import result_parts
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from app.agent import agent as agent_module
from app.agent.state import manager as state_manager_module
from app.agent.agent import Agent, AgentConfig
from app.agent.context import ContextStrategyConfig
from app.agent.models import vlm
from app.agent.state import AgentState
from app.agent.state.messages import HumanMessage, ToolCall
from app.agent.utils.domain import images as image_utils


class _UnusedLLM:
    """提供当前用例所需的 _UnusedLLM 测试替身，执行记录仅供外层断言使用。"""
    async def close(self) -> None:
        """接收测试对象的关闭请求，不释放任何生产资源。"""
        return None


def _make_agent(db_path: Path) -> Agent:
    """将用例提供的模型和临时存储组装成 Agent。

    Args:
        db_path (Path): 当前环境使用的 SQLite 数据库路径。

    Returns:
        Agent: 供外层用例断言使用的预设结果。
    """
    return Agent(
        AgentConfig(
            session_id="image-test",
            model_name="test",
            api_key="test",
            context_strategy=ContextStrategyConfig(compression_trigger_tokens_k=100),
        ),
        sqlite_factory=SqliteFactory(str(db_path)),
        llm_client=_UnusedLLM(),
    )


def _append_tool_image(state: AgentState, image: str) -> None:
    """向测试状态追加带关联编号的工具图片。

    Args:
        state (AgentState): 当前会话的内存 AgentState；保存由调用方负责。
        image (str): 本用例构造的图片内容。
    """
    call = ToolCall(id="call-draw", name="draw", args={})
    state.record_assistant_step("", (call,))
    state.record_tool_result(call, result_parts('done', (image,), 'tool'))


def test_vlm_default_config(monkeypatch) -> None:
    """验证定位模型使用声明的默认配置。

    Args:
        monkeypatch (pytest.MonkeyPatch): Pytest 提供的替换工具，用例结束后恢复环境变量和依赖。
    """
    monkeypatch.setenv("VLM_API_KEY", "test-key")
    monkeypatch.delenv("VLM_BASE_URL", raising=False)
    monkeypatch.delenv("VLM_MODEL", raising=False)

    assert vlm._vlm_config() == (
        "test-key",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "qwen3-vl-plus",
    )


def test_vlm_environment_overrides_defaults(monkeypatch) -> None:
    """验证定位模型环境设置覆盖默认值。

    Args:
        monkeypatch (pytest.MonkeyPatch): Pytest 提供的替换工具，用例结束后恢复环境变量和依赖。
    """
    monkeypatch.setenv("VLM_API_KEY", "test-key")
    monkeypatch.setenv("VLM_BASE_URL", "http://vlm.test/v1")
    monkeypatch.setenv("VLM_MODEL", "test-vlm")

    assert vlm._vlm_config() == (
        "test-key",
        "http://vlm.test/v1",
        "test-vlm",
    )


def test_vlm_locate_client_is_closed(monkeypatch) -> None:
    """验证定位完成后关闭模型客户端。

    Args:
        monkeypatch (pytest.MonkeyPatch): Pytest 提供的替换工具，用例结束后恢复环境变量和依赖。
    """
    clients = []
    requests = []

    class FakeCompletions:
        """提供当前用例所需的 FakeCompletions 测试替身，执行记录仅供外层断言使用。

        该定义仅用于外层用例：验证定位完成后关闭模型客户端。
        """
        async def create(self, **kwargs):
            """按用例脚本返回响应，并记录请求参数供外层断言。

            该定义仅用于外层用例：验证定位完成后关闭模型客户端。

            Args:
                kwargs (dict[str, Any]): 传递给被调用函数的其他关键字参数。
            """
            requests.append(kwargs)
            message = SimpleNamespace(content='{"bboxes_2d":[[1,2,3,4],[5,6,7,8]]}')
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    class FakeClient:
        """提供当前用例所需的 FakeClient 测试替身，执行记录仅供外层断言使用。

        该定义仅用于外层用例：验证定位完成后关闭模型客户端。
        """
        def __init__(self, **kwargs):
            """保存本用例的脚本输入和依赖，初始化测试对象的执行记录。

            该定义仅用于外层用例：验证定位完成后关闭模型客户端。

            Args:
                kwargs (dict[str, Any]): 传递给被调用函数的其他关键字参数。
            """
            self.chat = SimpleNamespace(completions=FakeCompletions())
            self.closed = False
            clients.append(self)

        async def close(self):
            """接收测试对象的关闭请求，不释放任何生产资源。

            该定义仅用于外层用例：验证定位完成后关闭模型客户端。
            """
            self.closed = True

    from app.agent.models import openai_chat
    monkeypatch.setattr(openai_chat, "AsyncOpenAI", FakeClient)
    monkeypatch.setenv("VLM_API_KEY", "test-key")

    result = asyncio.run(
        vlm.VLMService().locate_many(
            ["确认按钮", "输入框"],
            "data:image/png;base64,AA==",
        )
    )

    assert result == [[1, 2, 3, 4], [5, 6, 7, 8]]
    assert len(clients) == 1
    assert clients[0].closed is True
    assert len(requests) == 1
    parts = requests[0]["messages"][1]["content"]
    assert len([part for part in parts if part["type"] == "image_url"]) == 1
    text = next(part["text"] for part in parts if part["type"] == "text")
    assert json.loads(text) == {"targets": ["确认按钮", "输入框"]}


def test_save_multiple_images_writes_files(monkeypatch, tmp_path: Path) -> None:
    """验证多图片保存生成对应文件。

    Args:
        monkeypatch (pytest.MonkeyPatch): Pytest 提供的替换工具，用例结束后恢复环境变量和依赖。
        tmp_path (Path): Pytest 为当前用例创建的临时目录，不访问生产存储。
    """
    monkeypatch.setattr(image_utils, "get_images_dir", lambda: tmp_path)

    filenames = image_utils.save_multiple_images(
        ["data:image/png;base64,AA=="]
    )

    assert len(filenames) == 1
    assert (tmp_path / filenames[0]).read_bytes() == b"\x00"


def test_save_multiple_images_uses_restricted_subdirectory(monkeypatch, tmp_path: Path) -> None:
    """验证图片保存使用受限制的分类子目录。

    Args:
        monkeypatch (pytest.MonkeyPatch): Pytest 提供的替换工具，用例结束后恢复环境变量和依赖。
        tmp_path (Path): Pytest 为当前用例创建的临时目录，不访问生产存储。
    """
    monkeypatch.setattr(image_utils, "get_images_dir", lambda: tmp_path)

    filenames = image_utils.save_multiple_images(
        ["data:image/png;base64,AA=="],
        category="screenshot",
    )

    assert len(filenames) == 1
    assert filenames[0].startswith("screenshot/")
    assert (tmp_path / Path(filenames[0])).read_bytes() == b"\x00"


def test_save_multiple_images_rolls_back_partial_group(monkeypatch, tmp_path: Path) -> None:
    """验证一组图片部分保存失败时回滚已写文件。

    Args:
        monkeypatch (pytest.MonkeyPatch): Pytest 提供的替换工具，用例结束后恢复环境变量和依赖。
        tmp_path (Path): Pytest 为当前用例创建的临时目录，不访问生产存储。
    """
    monkeypatch.setattr(image_utils, "get_images_dir", lambda: tmp_path)
    calls = 0

    def save_one(image_data: str, *, category=None) -> str | None:
        """保存单张测试图片，并按用例要求在指定位置制造失败。

        该定义仅用于外层用例：验证一组图片部分保存失败时回滚已写文件。

        Args:
            image_data (str): 待模拟保存或定位的图片内容。
            category (Any): 真人、截图、本地工具或 MCP 图片的保存分类。

        Returns:
            str | None: 供外层用例断言使用的预设结果。
        """
        nonlocal calls
        calls += 1
        if calls == 1:
            path = tmp_path / "tool" / "first.png"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"first")
            return "tool/first.png"
        return None

    monkeypatch.setattr(image_utils, "save_image_to_disk", save_one)

    filenames = image_utils.save_multiple_images(
        ["data:image/png;base64,AA==", "data:image/png;base64,AQ=="],
        category="tool",
    )

    assert filenames == []
    assert not (tmp_path / "tool" / "first.png").exists()


def test_user_image_is_saved_before_first_protocol_commit(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """验证真人图片先保存文件再提交首条协议记录。

    Args:
        monkeypatch (pytest.MonkeyPatch): Pytest 提供的替换工具，用例结束后恢复环境变量和依赖。
        tmp_path (Path): Pytest 为当前用例创建的临时目录，不访问生产存储。
    """
    image = "data:image/png;base64,AA=="
    calls: list[list[str]] = []

    def save_images(images: list[str]) -> list[str]:
        """模拟图片保存并记录调用，用于检查消息提交先后顺序。

        该定义仅用于外层用例：验证真人图片先保存文件再提交首条协议记录。

        Args:
            images (list[str]): 当前需要模拟保存的图片集合。

        Returns:
            list[str]: 供外层用例断言使用的预设结果。
        """
        calls.append(list(images))
        return ["stored.png"]

    monkeypatch.setattr(
        state_manager_module,
        "save_multiple_images",
        save_images,
        raising=False,
    )

    async def scenario() -> tuple[AgentState, tuple[str, str | None]]:
        """由外层测试调用，完成“真人图片先保存文件再提交首条协议记录”的异步步骤。

        Returns:
            tuple[AgentState, tuple[str, str | None]]: 供外层用例断言使用的预设结果。
        """
        agent = _make_agent(tmp_path / "agent.sqlite3")
        try:
            state = AgentState.create_new("image-test")
            async for _ in agent._commit_user_input(
                state,
                "看看这张图",
                [image],
                activation_data={},
            ):
                pass
            db = await agent.session_state_manager._state_dao._get_db()
            row = (await db.execute_fetchall(
                "SELECT message_json, CASE WHEN json_extract(message_json,'$.content[1].storage_ref') IS NULL THEN NULL ELSE json_array(json_extract(message_json,'$.content[1].storage_ref')) END FROM conversation_messages"
            ))[0]
            return state, row
        finally:
            await agent.session_state_manager.close()

    state, row = asyncio.run(scenario())
    message = state.messages[-1]
    payload = json.loads(row[0])

    assert calls == [[image]]
    assert isinstance(message, HumanMessage)
    assert tuple(part.storage_ref for part in message.content if isinstance(part, ImageContent) and part.storage_ref) == ("stored.png",)
    assert "image_filenames" not in payload
    assert payload["content"][1]["image_url"]["url"] == "stored.png"
    assert json.loads(row[1]) == ["stored.png"]


def test_text_input_does_not_save_images(monkeypatch, tmp_path: Path) -> None:
    """验证纯文本输入不调用图片保存。

    Args:
        monkeypatch (pytest.MonkeyPatch): Pytest 提供的替换工具，用例结束后恢复环境变量和依赖。
        tmp_path (Path): Pytest 为当前用例创建的临时目录，不访问生产存储。
    """
    def unexpected_save(images: list[str]) -> list[str]:
        """纯文本处理触发图片保存时主动失败，验证没有无必要写文件。

        该定义仅用于外层用例：验证纯文本输入不调用图片保存。

        Args:
            images (list[str]): 当前需要模拟保存的图片集合。
        """
        raise AssertionError("纯文本输入不应保存图片")

    monkeypatch.setattr(
        state_manager_module,
        "save_multiple_images",
        unexpected_save,
        raising=False,
    )

    async def scenario() -> AgentState:
        """由外层测试调用，完成“纯文本输入不调用图片保存”的异步步骤。

        Returns:
            AgentState: 供外层用例断言使用的预设结果。
        """
        agent = _make_agent(tmp_path / "agent.sqlite3")
        try:
            state = AgentState.create_new("image-test")
            async for _ in agent._commit_user_input(
                state,
                "只有文字",
                None,
                activation_data={},
            ):
                pass
            return state
        finally:
            await agent.session_state_manager.close()

    state = asyncio.run(scenario())
    assert isinstance(state.messages[-1], HumanMessage)
    assert state.messages[-1].text == "只有文字"
    assert tuple(part.storage_ref for part in state.messages[-1].content if isinstance(part, ImageContent) and part.storage_ref) == ()


def test_image_save_failure_does_not_block_user_commit(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """验证图片保存失败不阻止真人输入提交。

    Args:
        monkeypatch (pytest.MonkeyPatch): Pytest 提供的替换工具，用例结束后恢复环境变量和依赖。
        tmp_path (Path): Pytest 为当前用例创建的临时目录，不访问生产存储。
    """
    called = False

    def fail_save(images: list[str]) -> list[str]:
        """主动制造图片保存异常，验证提交和历史保留的降级行为。

        该定义仅用于外层用例：验证图片保存失败不阻止真人输入提交。

        Args:
            images (list[str]): 当前需要模拟保存的图片集合。
        """
        nonlocal called
        called = True
        raise OSError("disk unavailable")

    monkeypatch.setattr(
        state_manager_module,
        "save_multiple_images",
        fail_save,
        raising=False,
    )

    async def scenario() -> tuple[AgentState, tuple[str, str | None]]:
        """由外层测试调用，完成“图片保存失败不阻止真人输入提交”的异步步骤。

        Returns:
            tuple[AgentState, tuple[str, str | None]]: 供外层用例断言使用的预设结果。
        """
        agent = _make_agent(tmp_path / "agent.sqlite3")
        try:
            state = AgentState.create_new("image-test")
            async for _ in agent._commit_user_input(
                state,
                "继续聊天",
                ["data:image/png;base64,AA=="],
                activation_data={},
            ):
                pass
            db = await agent.session_state_manager._state_dao._get_db()
            row = (await db.execute_fetchall(
                "SELECT message_json, CASE WHEN json_extract(message_json,'$.content[1].storage_ref') IS NULL THEN NULL ELSE json_array(json_extract(message_json,'$.content[1].storage_ref')) END FROM conversation_messages"
            ))[0]
            return state, row
        finally:
            await agent.session_state_manager.close()

    state, row = asyncio.run(scenario())
    payload = json.loads(row[0])

    assert called is True
    assert payload["content"][0]["text"] == "继续聊天"
    assert payload["content"][1]["image_url"]["url"] == "data:image/png;base64,AA=="
    assert isinstance(state.messages[-1], HumanMessage)
    assert tuple(part.storage_ref for part in state.messages[-1].content if isinstance(part, ImageContent) and part.storage_ref) == ()
    assert row[1] is None


def test_tool_image_is_externalized_for_protocol_but_kept_in_checkpoint(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """验证工具图片协议日志外置但 checkpoint 保留模型地址。

    Args:
        monkeypatch (pytest.MonkeyPatch): Pytest 提供的替换工具，用例结束后恢复环境变量和依赖。
        tmp_path (Path): Pytest 为当前用例创建的临时目录，不访问生产存储。
    """
    image = "data:image/png;base64,AA=="
    monkeypatch.setattr(image_utils, "get_images_dir", lambda: tmp_path / "images")

    async def scenario() -> tuple[dict, dict, list[str]]:
        """由外层测试调用，完成“工具图片协议日志外置但 checkpoint 保留模型地址”的异步步骤。

        Returns:
            tuple[dict, dict, list[str]]: 供外层用例断言使用的预设结果。
        """
        manager = agent_module.SessionStateManager("tool-image", sqlite_factory=SqliteFactory(str(tmp_path / 'agent.sqlite3')))
        try:
            state = AgentState.create_new("tool-image")
            _append_tool_image(state, image)
            await manager.save(state, checkpoint_type="completed")
            db = await manager._state_dao._get_db()
            protocol_raw, attachments_raw = (await db.execute_fetchall(
                "SELECT message_json, CASE WHEN json_extract(message_json,'$.content[1].storage_ref') IS NULL THEN NULL ELSE json_array(json_extract(message_json,'$.content[1].storage_ref')) END FROM conversation_messages WHERE message_type='tool'"
            ))[0]
            checkpoint_raw = (await db.execute_fetchall(
                "SELECT state_json FROM checkpoints ORDER BY id DESC LIMIT 1"
            ))[0][0]
            return json.loads(protocol_raw), json.loads(checkpoint_raw), json.loads(attachments_raw)
        finally:
            await manager.close()

    protocol, checkpoint, attachments = asyncio.run(scenario())

    assert attachments[0].startswith("tool/")
    assert protocol["content"][1]["media_kind"] == "tool"
    assert protocol["content"][1]["image_url"]["url"] == attachments[0]
    assert checkpoint["messages"][-1]["content"][1]["image_url"]["url"] == image
    assert (tmp_path / "images" / Path(attachments[0])).read_bytes() == b"\x00"


def test_tool_image_protocol_row_upgrades_after_transient_save_failure(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """验证临时保存失败后的图片协议行可升级为文件引用。

    Args:
        monkeypatch (pytest.MonkeyPatch): Pytest 提供的替换工具，用例结束后恢复环境变量和依赖。
        tmp_path (Path): Pytest 为当前用例创建的临时目录，不访问生产存储。
    """
    attempts = 0

    def save_images(images: list[str], *, category=None) -> list[str]:
        """模拟图片保存并记录调用，用于检查消息提交先后顺序。

        该定义仅用于外层用例：验证临时保存失败后的图片协议行可升级为文件引用。

        Args:
            images (list[str]): 当前需要模拟保存的图片集合。
            category (Any): 真人、截图、本地工具或 MCP 图片的保存分类。

        Returns:
            list[str]: 供外层用例断言使用的预设结果。
        """
        nonlocal attempts
        attempts += 1
        return [] if attempts == 1 else ["tool/recovered.png"]

    monkeypatch.setattr(state_manager_module, "save_multiple_images", save_images)

    async def scenario() -> tuple[dict, str | None, dict, str | None]:
        """由外层测试调用，完成“临时保存失败后的图片协议行可升级为文件引用”的异步步骤。

        Returns:
            tuple[dict, str | None, dict, str | None]: 供外层用例断言使用的预设结果。
        """
        manager = state_manager_module.SessionStateManager(
            "retry-tool-image",
            sqlite_factory=SqliteFactory(str(tmp_path / 'agent.sqlite3')),
        )
        try:
            state = AgentState.create_new("retry-tool-image")
            _append_tool_image(state, "data:image/png;base64,AA==")
            await manager.save(state, checkpoint_type="completed")
            db = await manager._state_dao._get_db()
            first_raw, first_attachments = (await db.execute_fetchall(
                "SELECT message_json, CASE WHEN json_extract(message_json,'$.content[1].storage_ref') IS NULL THEN NULL ELSE json_array(json_extract(message_json,'$.content[1].storage_ref')) END FROM conversation_messages WHERE message_type='tool'"
            ))[0]

            await manager.save(state, checkpoint_type="completed")
            second_raw, second_attachments = (await db.execute_fetchall(
                "SELECT message_json, CASE WHEN json_extract(message_json,'$.content[1].storage_ref') IS NULL THEN NULL ELSE json_array(json_extract(message_json,'$.content[1].storage_ref')) END FROM conversation_messages WHERE message_type='tool'"
            ))[0]
            return (
                json.loads(first_raw),
                first_attachments,
                json.loads(second_raw),
                second_attachments,
            )
        finally:
            await manager.close()

    first, first_attachments, second, second_attachments = asyncio.run(scenario())

    assert first["content"][1]["image_url"]["url"].startswith("data:image/")
    assert first_attachments is None
    assert second["content"][1]["image_url"]["url"] == "tool/recovered.png"
    assert json.loads(second_attachments) == ["tool/recovered.png"]


def test_tool_image_save_exception_preserves_base64_and_checkpoint(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """验证工具图片外置异常时保留 Base64 和 checkpoint。

    Args:
        monkeypatch (pytest.MonkeyPatch): Pytest 提供的替换工具，用例结束后恢复环境变量和依赖。
        tmp_path (Path): Pytest 为当前用例创建的临时目录，不访问生产存储。
    """
    def fail_save(_images: list[str], *, category=None) -> list[str]:
        """主动制造图片保存异常，验证提交和历史保留的降级行为。

        该定义仅用于外层用例：验证工具图片外置异常时保留 Base64 和 checkpoint。

        Args:
            _images (list[str]): 当前需要模拟保存的图片集合。
            category (Any): 真人、截图、本地工具或 MCP 图片的保存分类。
        """
        raise OSError("disk unavailable")

    monkeypatch.setattr(state_manager_module, "save_multiple_images", fail_save)

    async def scenario() -> tuple[dict, dict, str | None]:
        """由外层测试调用，完成“工具图片外置异常时保留 Base64 和 checkpoint”的异步步骤。

        Returns:
            tuple[dict, dict, str | None]: 供外层用例断言使用的预设结果。
        """
        manager = state_manager_module.SessionStateManager(
            "failed-tool-image",
            sqlite_factory=SqliteFactory(str(tmp_path / 'agent.sqlite3')),
        )
        try:
            state = AgentState.create_new("failed-tool-image")
            _append_tool_image(state, "data:image/png;base64,AA==")
            await manager.save(state, checkpoint_type="completed")
            db = await manager._state_dao._get_db()
            protocol_raw, attachments = (await db.execute_fetchall(
                "SELECT message_json, CASE WHEN json_extract(message_json,'$.content[1].storage_ref') IS NULL THEN NULL ELSE json_array(json_extract(message_json,'$.content[1].storage_ref')) END FROM conversation_messages WHERE message_type='tool'"
            ))[0]
            checkpoint_raw = (await db.execute_fetchall(
                "SELECT state_json FROM checkpoints ORDER BY id DESC LIMIT 1"
            ))[0][0]
            return json.loads(protocol_raw), json.loads(checkpoint_raw), attachments
        finally:
            await manager.close()

    protocol, checkpoint, attachments = asyncio.run(scenario())

    expected = "data:image/png;base64,AA=="
    assert protocol["content"][1]["image_url"]["url"] == expected
    assert checkpoint["messages"][-1]["content"][1]["image_url"]["url"] == expected
    assert attachments is None
