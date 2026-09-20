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
from app.agent.utils.domain.text import normalize_messages_for_model


class _UnusedLLM:
    async def close(self) -> None:
        return None


def _make_agent(db_path: Path) -> Agent:
    return Agent(
        AgentConfig(
            session_id="image-test",
            model_name="test",
            api_key="test",
            context_strategy=ContextStrategyConfig(compression_trigger_tokens_k=100),
        ),
        db_path=str(db_path),
        llm_client=_UnusedLLM(),
    )


def _append_tool_image(state: AgentState, image: str) -> None:
    call = ToolCall(id="call-draw", name="draw", args={})
    state.record_assistant_step("", (call,))
    state.record_tool_result(call, "done", image_urls=(image,))


def test_vlm_default_config(monkeypatch) -> None:
    monkeypatch.setenv("VLM_API_KEY", "test-key")
    monkeypatch.delenv("VLM_BASE_URL", raising=False)
    monkeypatch.delenv("VLM_MODEL", raising=False)

    assert vlm._vlm_config() == (
        "test-key",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "qwen3-vl-plus",
    )


def test_vlm_environment_overrides_defaults(monkeypatch) -> None:
    monkeypatch.setenv("VLM_API_KEY", "test-key")
    monkeypatch.setenv("VLM_BASE_URL", "http://vlm.test/v1")
    monkeypatch.setenv("VLM_MODEL", "test-vlm")

    assert vlm._vlm_config() == (
        "test-key",
        "http://vlm.test/v1",
        "test-vlm",
    )


def test_vlm_locate_client_is_closed(monkeypatch) -> None:
    clients = []

    class FakeCompletions:
        async def create(self, **kwargs):
            message = SimpleNamespace(content='{"bbox_2d":[1,2,3,4]}')
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    class FakeClient:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=FakeCompletions())
            self.closed = False
            clients.append(self)

        async def close(self):
            self.closed = True

    monkeypatch.setattr(vlm, "AsyncOpenAI", FakeClient)
    monkeypatch.setenv("VLM_API_KEY", "test-key")

    result = asyncio.run(
        vlm.VLMService().locate(
            "确认按钮",
            "data:image/png;base64,AA==",
        )
    )

    assert result == [1, 2, 3, 4]
    assert len(clients) == 1
    assert clients[0].closed is True


def test_save_multiple_images_writes_files(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(image_utils, "get_images_dir", lambda: tmp_path)

    filenames = image_utils.save_multiple_images(
        ["data:image/png;base64,AA=="]
    )

    assert len(filenames) == 1
    assert (tmp_path / filenames[0]).read_bytes() == b"\x00"


def test_save_multiple_images_uses_restricted_subdirectory(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(image_utils, "get_images_dir", lambda: tmp_path)

    filenames = image_utils.save_multiple_images(
        ["data:image/png;base64,AA=="],
        category="screenshot",
    )

    assert len(filenames) == 1
    assert filenames[0].startswith("screenshot/")
    assert (tmp_path / Path(filenames[0])).read_bytes() == b"\x00"


def test_save_multiple_images_rolls_back_partial_group(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(image_utils, "get_images_dir", lambda: tmp_path)
    calls = 0

    def save_one(image_data: str, *, category=None) -> str | None:
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


def test_legacy_image_description_is_filtered_from_model_input() -> None:
    normalized = normalize_messages_for_model([
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "历史消息"},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,AA=="},
                },
            ],
            "image_description": "旧图片描述",
            "image_filenames": ["legacy.png"],
        }
    ])

    assert "image_description" not in normalized[0]
    assert "image_filenames" not in normalized[0]
    assert normalized[0]["content"][1]["type"] == "image_url"


def test_user_image_is_saved_before_first_protocol_commit(
    monkeypatch,
    tmp_path: Path,
) -> None:
    image = "data:image/png;base64,AA=="
    calls: list[list[str]] = []

    def save_images(images: list[str]) -> list[str]:
        calls.append(list(images))
        return ["stored.png"]

    monkeypatch.setattr(
        agent_module,
        "save_multiple_images",
        save_images,
        raising=False,
    )
    monkeypatch.delenv("VLM_API_KEY", raising=False)

    async def scenario() -> tuple[AgentState, tuple[str, str | None]]:
        agent = _make_agent(tmp_path / "agent.sqlite3")
        try:
            state = await agent._commit_user_input(
                AgentState.create_new("image-test"),
                "看看这张图",
                [image],
            )
            db = await agent.state_manager._get_db()
            row = (await db.execute_fetchall(
                "SELECT message_json, attachments_json FROM conversation_messages"
            ))[0]
            return state, row
        finally:
            await agent.state_manager.close()

    state, row = asyncio.run(scenario())
    message = state.messages[-1]
    payload = json.loads(row[0])

    assert calls == [[image]]
    assert isinstance(message, HumanMessage)
    assert message.image_filenames == ("stored.png",)
    assert "image_filenames" not in payload
    assert payload["content"][1]["image_url"]["url"] == "stored.png"
    assert "image_description" not in payload
    assert json.loads(row[1]) == ["stored.png"]
    assert "image_task_key" not in state.extra


def test_text_input_does_not_save_images(monkeypatch, tmp_path: Path) -> None:
    def unexpected_save(images: list[str]) -> list[str]:
        raise AssertionError("纯文本输入不应保存图片")

    monkeypatch.setattr(
        agent_module,
        "save_multiple_images",
        unexpected_save,
        raising=False,
    )

    async def scenario() -> AgentState:
        agent = _make_agent(tmp_path / "agent.sqlite3")
        try:
            return await agent._commit_user_input(
                AgentState.create_new("image-test"),
                "只有文字",
                None,
            )
        finally:
            await agent.state_manager.close()

    state = asyncio.run(scenario())
    assert isinstance(state.messages[-1], HumanMessage)
    assert state.messages[-1].text == "只有文字"
    assert state.messages[-1].image_filenames == ()


def test_image_save_failure_does_not_block_user_commit(
    monkeypatch,
    tmp_path: Path,
) -> None:
    called = False

    def fail_save(images: list[str]) -> list[str]:
        nonlocal called
        called = True
        raise OSError("disk unavailable")

    monkeypatch.setattr(
        agent_module,
        "save_multiple_images",
        fail_save,
        raising=False,
    )

    async def scenario() -> tuple[AgentState, tuple[str, str | None]]:
        agent = _make_agent(tmp_path / "agent.sqlite3")
        try:
            state = await agent._commit_user_input(
                AgentState.create_new("image-test"),
                "继续聊天",
                ["data:image/png;base64,AA=="],
            )
            db = await agent.state_manager._get_db()
            row = (await db.execute_fetchall(
                "SELECT message_json, attachments_json FROM conversation_messages"
            ))[0]
            return state, row
        finally:
            await agent.state_manager.close()

    state, row = asyncio.run(scenario())
    payload = json.loads(row[0])

    assert called is True
    assert payload["content"][0]["text"] == "继续聊天"
    assert payload["content"][1]["image_url"]["url"] == "data:image/png;base64,AA=="
    assert isinstance(state.messages[-1], HumanMessage)
    assert state.messages[-1].image_filenames == ()
    assert row[1] is None


def test_tool_image_is_externalized_for_protocol_but_kept_in_checkpoint(
    monkeypatch,
    tmp_path: Path,
) -> None:
    image = "data:image/png;base64,AA=="
    monkeypatch.setattr(image_utils, "get_images_dir", lambda: tmp_path / "images")

    async def scenario() -> tuple[dict, dict, list[str]]:
        manager = agent_module.StateManager("tool-image", db_path=str(tmp_path / "agent.sqlite3"))
        try:
            state = AgentState.create_new("tool-image")
            _append_tool_image(state, image)
            await manager.save(state, checkpoint_type="completed")
            db = await manager._get_db()
            protocol_raw, attachments_raw = (await db.execute_fetchall(
                "SELECT message_json, attachments_json FROM conversation_messages WHERE record_kind='tool_image'"
            ))[0]
            checkpoint_raw = (await db.execute_fetchall(
                "SELECT state_json FROM checkpoints ORDER BY id DESC LIMIT 1"
            ))[0][0]
            return json.loads(protocol_raw), json.loads(checkpoint_raw), json.loads(attachments_raw)
        finally:
            await manager.close()

    protocol, checkpoint, attachments = asyncio.run(scenario())

    assert attachments[0].startswith("tool/")
    assert protocol["name"] == "system_tool_image"
    assert protocol["content"][1]["image_url"]["url"] == attachments[0]
    assert checkpoint["messages"][-1]["content"][1]["image_url"]["url"] == image
    assert (tmp_path / "images" / Path(attachments[0])).read_bytes() == b"\x00"


def test_tool_image_protocol_row_upgrades_after_transient_save_failure(
    monkeypatch,
    tmp_path: Path,
) -> None:
    attempts = 0

    def save_images(images: list[str], *, category=None) -> list[str]:
        nonlocal attempts
        attempts += 1
        return [] if attempts == 1 else ["tool/recovered.png"]

    monkeypatch.setattr(state_manager_module, "save_multiple_images", save_images)

    async def scenario() -> tuple[dict, str | None, dict, str | None]:
        manager = state_manager_module.StateManager(
            "retry-tool-image",
            db_path=str(tmp_path / "agent.sqlite3"),
        )
        try:
            state = AgentState.create_new("retry-tool-image")
            _append_tool_image(state, "data:image/png;base64,AA==")
            await manager.save(state, checkpoint_type="completed")
            db = await manager._get_db()
            first_raw, first_attachments = (await db.execute_fetchall(
                "SELECT message_json, attachments_json FROM conversation_messages WHERE record_kind='tool_image'"
            ))[0]

            await manager.save(state, checkpoint_type="completed")
            second_raw, second_attachments = (await db.execute_fetchall(
                "SELECT message_json, attachments_json FROM conversation_messages WHERE record_kind='tool_image'"
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
    def fail_save(_images: list[str], *, category=None) -> list[str]:
        raise OSError("disk unavailable")

    monkeypatch.setattr(state_manager_module, "save_multiple_images", fail_save)

    async def scenario() -> tuple[dict, dict, str | None]:
        manager = state_manager_module.StateManager(
            "failed-tool-image",
            db_path=str(tmp_path / "agent.sqlite3"),
        )
        try:
            state = AgentState.create_new("failed-tool-image")
            _append_tool_image(state, "data:image/png;base64,AA==")
            await manager.save(state, checkpoint_type="completed")
            db = await manager._get_db()
            protocol_raw, attachments = (await db.execute_fetchall(
                "SELECT message_json, attachments_json FROM conversation_messages WHERE record_kind='tool_image'"
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
