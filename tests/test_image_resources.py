import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from app.agent import agent as agent_module
from app.agent.agent import Agent, AgentConfig
from app.agent.context_strategy import ContextStrategyConfig
from app.agent.models import vlm
from app.agent.state import AgentState
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
    assert message["image_filenames"] == ["stored.png"]
    assert "image_description" not in message
    assert payload["image_filenames"] == ["stored.png"]
    assert payload["content"][1]["image_url"]["url"] == image
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
    assert state.messages[-1]["content"] == "只有文字"
    assert "image_filenames" not in state.messages[-1]


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
    assert "image_filenames" not in state.messages[-1]
    assert row[1] is None
