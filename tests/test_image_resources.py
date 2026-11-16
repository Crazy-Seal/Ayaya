from app.crud.sqlite_factory import SqliteFactory
from app.agent.state.messages import ImageContent
from tests.model_capture import result_parts
import asyncio
import json
import pytest
from pathlib import Path
from types import SimpleNamespace

from app.agent import agent as agent_module
from app.agent.state import manager as state_manager_module
from app.agent.agent import Agent, AgentConfig
from app.agent.context import ContextStrategyConfig
from app.agent.state import AgentState
from app.agent.state.messages import HumanMessage, ToolCall
from app.agent.utils.domain import images as image_utils


def test_saved_image_roundtrips_with_reference_only() -> None:
    """已保存的工具图片通过消息协议往返时只保留文件引用。"""
    from app.agent.state.messages import content_from_json

    image = ImageContent(storage_ref="tool/example.png", media_kind="tool", detail="low")
    payload = image.to_dict()
    assert payload == {"type": "image_url", "image_url": {"detail": "low"},
                       "media_kind": "tool", "storage_ref": "tool/example.png"}
    assert content_from_json([payload]) == (image,)
    with pytest.raises(ValueError):
        ImageContent()


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
        category="environment",
    )

    assert len(filenames) == 1
    assert filenames[0].startswith("environment/")
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
    assert payload["content"][1]["storage_ref"] == "stored.png"
    assert "url" not in payload["content"][1]["image_url"]
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


@pytest.mark.parametrize("source", ["human", "tool"])
@pytest.mark.parametrize("raises", [False, True])
def test_image_save_failure_blocks_database_commit(monkeypatch, tmp_path: Path, source: str, raises: bool) -> None:
    """附件或工具图片保存失败时，当前轮次进入必需保存故障出口。

    Args:
        monkeypatch (pytest.MonkeyPatch): 替换文件写入以制造故障。
        tmp_path (Path): 隔离数据库目录。
        source (str): 当前验证真人附件或工具图片。
        raises (bool): 文件写入是否抛出异常。
    """
    def fail_save(images: list[str], *, category=None) -> list[str]:
        """模拟磁盘写入失败，覆盖返回空结果与抛异常两种出口。

        Args:
            images (list[str]): 本次待写入的图片。
            category (MediaKind | None): 图片来源目录。
        Returns:
            list[str]: 文件保存失败的空结果。
        Raises:
            OSError: 用例要求模拟磁盘异常。
        """
        if raises:
            raise OSError("disk unavailable")
        return []

    monkeypatch.setattr(state_manager_module, "save_multiple_images", fail_save)

    async def scenario() -> None:
        """通过公开保存入口确认失败后没有历史和 checkpoint 提交。"""
        manager = state_manager_module.SessionStateManager("failure", SqliteFactory(tmp_path / "state.sqlite3"))
        state = AgentState.create_new("failure")
        if source == "human":
            state.add_user_message("附件", image_urls=("data:image/png;base64,AA==",))
        else:
            _append_tool_image(state, "data:image/png;base64,AA==")
        try:
            with pytest.raises(state_manager_module.RequiredSaveError):
                await manager.save_progress(state)
            assert (await manager.load()).messages == ()
        finally:
            await manager.close()

    asyncio.run(scenario())


def test_image_save_can_retry_before_first_history_commit(monkeypatch, tmp_path: Path) -> None:
    """首次图片写入失败后，重试保存仍将完整原始观察归档为文件引用。

    Args:
        monkeypatch (pytest.MonkeyPatch): 仅让首次磁盘保存失败。
        tmp_path (Path): 隔离数据库和图片目录。
    """
    original = image_utils.save_multiple_images
    attempts = 0

    def save_images(images: list[str], *, category=None) -> list[str]:
        """第一次写入失败，后续使用真实文件保存流程。

        Args:
            images (list[str]): 当前待保存图片。
            category (MediaKind | None): 图片来源目录。
        Returns:
            list[str]: 文件引用，首次失败时为空。
        """
        nonlocal attempts
        attempts += 1
        return [] if attempts == 1 else original(images, category=category)

    monkeypatch.setattr(state_manager_module, "save_multiple_images", save_images)

    async def scenario() -> None:
        """先失败再保存，并通过恢复入口核验引用图片。"""
        manager = state_manager_module.SessionStateManager("retry", SqliteFactory(tmp_path / "state.sqlite3"))
        state = AgentState.create_new("retry")
        _append_tool_image(state, "data:image/png;base64,AA==")
        try:
            with pytest.raises(state_manager_module.RequiredSaveError):
                await manager.save_progress(state)
            assert (await manager.load()).messages == ()
            await manager.save_progress(state)
            restored = await manager.load()
            picture = restored.messages[-1].content[-1]
            assert isinstance(picture, ImageContent)
            assert picture.storage_ref.startswith("tool/") and picture.url is None
        finally:
            await manager.close()

    asyncio.run(scenario())
