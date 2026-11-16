"""验证临时环境图片的模型请求、审批恢复材料和轮次结束清理。"""
from app.crud.sqlite_factory import SqliteFactory

from openai import AsyncOpenAI
from typing import cast

import asyncio
from contextlib import closing
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from app.agent.context._projection import append_transient_context, build_model_window
from app.agent.models.contracts import LLMConfig
from app.agent.models.openai_chat import OpenAIChatCompletionsAdapter
from app.agent.state import AgentState, TemporaryContextItem
from app.agent.state.manager import SessionStateManager
from app.agent.state.messages import (
    EnvironmentPerceptionMessage,
    ImageContent,
    message_from_storage_format,
    text_content,
)
from app.agent.utils.domain.token_estimation import RoughTokenEstimator
from app.schemas.message_types import MediaKind


@pytest.fixture
def adapter() -> OpenAIChatCompletionsAdapter:
    """提供只编码请求和估算预算的模型适配器。

    Returns:
        OpenAIChatCompletionsAdapter: 使用测试 SDK 替身、不访问模型服务的适配器。
    """
    return OpenAIChatCompletionsAdapter(LLMConfig("test", "test"), sdk_client=cast(AsyncOpenAI, SimpleNamespace()))


def test_environment_images_preserve_item_order_in_model_request(adapter: OpenAIChatCompletionsAdapter) -> None:
    """多个来源提供环境图文时，每项图片紧随对应正文，模型请求保持独立环境消息。

    Args:
        adapter (OpenAIChatCompletionsAdapter): 观察实际请求编码的测试适配器。
    """
    state = AgentState.create_new("environment-images")
    state.add_user_message("检查当前环境")
    image_a = ImageContent("data:image/png;base64,AA==", media_kind=MediaKind.ENVIRONMENT, detail="low")
    image_b = ImageContent("data:image/png;base64,AQ==", media_kind=MediaKind.ENVIRONMENT)
    # 1. 故意反向插入，确认请求按条目标识排序，并保留每项图片的归属。
    state.set_temporary_context("plugin.b", TemporaryContextItem(title="窗口乙", images=(image_b,)))
    state.set_temporary_context("plugin.a", TemporaryContextItem(title="窗口甲", text="当前页面", images=(image_a,)))
    original = state.messages
    projected = append_transient_context(state)
    encoded = adapter.encode_messages(build_model_window(projected))

    assert len(encoded) == 2
    assert encoded[-1] == {
        "role": "user", "name": "runtime_environment_perception", "content": [
            {"type": "text", "text": "[环境感知]\n"},
            {"type": "text", "text": "[窗口甲]\n当前页面"},
            {"type": "image_url", "image_url": {"url": image_a.url, "detail": "low"}},
            {"type": "text", "text": "[窗口乙]\n"},
            {"type": "image_url", "image_url": {"url": image_b.url}},
        ],
    }
    assert state.messages == original
    assert not projected[-1].enters_protocol_log
    assert message_from_storage_format(json.loads(json.dumps(projected[-1].to_storage_format()))) == projected[-1]


def test_text_environment_request_keeps_existing_format(adapter: OpenAIChatCompletionsAdapter) -> None:
    """已有时间和记忆插件只提供文本时，继续发送原有字符串正文。

    Args:
        adapter (OpenAIChatCompletionsAdapter): 观察实际请求编码的测试适配器。
    """
    state = AgentState.create_new("text-environment")
    state.set_temporary_context("memory.recalled", TemporaryContextItem(title="相关记忆", text="用户偏好"))
    state.set_temporary_context("core.current_time", TemporaryContextItem(title="当前时间", text="今天"))
    assert adapter.encode_messages(append_transient_context(state)) == [{
        "role": "user", "name": "runtime_environment_perception",
        "content": "[环境感知]\n[当前时间]\n今天\n\n[相关记忆]\n用户偏好",
    }]


def test_environment_image_budget_counts_images_instead_of_base64(adapter: OpenAIChatCompletionsAdapter) -> None:
    """环境图片按图片开销估算，增加 Base64 长度不会被当作正文 token。

    Args:
        adapter (OpenAIChatCompletionsAdapter): 使用真实请求结构估算预算的测试适配器。
    """
    image = ImageContent("data:image/png;base64,AA==", media_kind=MediaKind.ENVIRONMENT)
    message = EnvironmentPerceptionMessage(content=(*text_content("页面"), image))
    large_image = replace(image, url="data:image/png;base64," + "AAAA" * 10_000)
    larger = replace(message, content=(*text_content("页面"), large_image))
    second_image = replace(message, content=(*message.content, large_image))
    assert adapter.estimate_input((message,)) == adapter.estimate_input((larger,))
    assert adapter.estimate_input((second_image,)) - adapter.estimate_input((message,)) == (
        RoughTokenEstimator.IMAGE_TOKENS + RoughTokenEstimator.CONTENT_PART_OVERHEAD
    )


@pytest.mark.parametrize("ending", ["complete", "stop", "abandoned"])
def test_environment_images_restore_without_entering_history_and_clear_on_end(
    tmp_path: Path, adapter: OpenAIChatCompletionsAdapter, ending: str,
) -> None:
    """轮内进度保留环境图片供恢复使用，聊天历史只保存真人消息，轮次结束清理背景。

    Args:
        tmp_path (Path): Pytest 创建的隔离目录。
        adapter (OpenAIChatCompletionsAdapter): 检查恢复后模型输入的测试适配器。
        ending (str): 正常完成、用户停止或退出后整理旧轮次。
    """
    async def scenario() -> None:
        """在隔离数据库中验证图片保存、恢复和轮次结束的完整流程。"""
        db_path = tmp_path / "state.sqlite3"
        manager = SessionStateManager("environment-checkpoint", SqliteFactory(str(db_path)))
        state = AgentState.create_new("environment-checkpoint")
        state.add_user_message("查看当前页面")
        image = ImageContent("data:image/png;base64,AA==", media_kind=MediaKind.ENVIRONMENT)
        item = TemporaryContextItem(title="当前页面", text="环境背景", images=(image,))
        state.set_temporary_context("plugin.screen", item)
        try:
            # 1. 模拟轮内保存进度和审批恢复，经过 JSON 与数据库往返后仍可发送原图片。
            await manager.save_progress(state)
            restored = await manager.load()
            saved_image = restored.temporary_context["plugin.screen"].images[0]
            assert saved_image.url is None and saved_image.storage_ref.startswith("environment/")
            assert restored.temporary_context["plugin.screen"].images[0].media_kind is MediaKind.ENVIRONMENT
            from app.agent.models.llm_client import LLMClient
            assert LLMClient(LLMConfig("test", "test"), adapter=adapter).estimate_input(
                append_transient_context(restored)) == adapter.estimate_input(append_transient_context(state))
            # 2. 背景只保存在 checkpoint 的临时条目，协议历史中没有环境消息和图片数据。
            with closing(sqlite3.connect(db_path)) as connection:
                history = connection.execute("SELECT message_type, message_json FROM conversation_messages").fetchall()
                assert [row[0] for row in history] == ["human"]
                assert image.url not in history[0][1] and "环境背景" not in history[0][1]
                checkpoint = json.loads(connection.execute("SELECT state_json FROM checkpoints").fetchone()[0])
                assert checkpoint["temporary_context"]["plugin.screen"]["images"][0]["storage_ref"] == saved_image.storage_ref
                assert "base64" not in json.dumps(checkpoint)

            # 3. 三种结束路径均清理临时图片，并删除数据库中的旧中间态。
            if ending == "complete":
                await manager.complete_turn(restored)
            elif ending == "stop":
                from app.schemas.runtime_types import TurnInterruptReason
                await manager.finish_interrupted_turn(restored, reason=TurnInterruptReason.STOP,
                    partial="", partial_persisted=False, origin_channel="desktop")
            else:
                assert await manager.finalize_abandoned_turn()
            assert (await manager.load()).temporary_context == {}
            with closing(sqlite3.connect(db_path)) as connection:
                checkpoints = connection.execute("SELECT checkpoint_type, state_json FROM checkpoints").fetchall()
                assert [row[0] for row in checkpoints] == ["completed"]
                assert all(image.url not in row[1] for row in checkpoints)
        finally:
            await manager.close()

    asyncio.run(scenario())


def test_environment_image_updates_and_removal_replace_old_background(adapter: OpenAIChatCompletionsAdapter) -> None:
    """插件更新同一条目时替换旧图片，删除条目后请求恢复为原历史。

    Args:
        adapter (OpenAIChatCompletionsAdapter): 观察每次请求实际图片的测试适配器。
    """
    state = AgentState.create_new("updated-environment")
    old_image = ImageContent("data:image/png;base64,AA==", media_kind=MediaKind.ENVIRONMENT)
    new_image = ImageContent("data:image/png;base64,AQ==", media_kind=MediaKind.ENVIRONMENT)
    state.set_temporary_context("plugin.screen", TemporaryContextItem(title="页面", images=(old_image,)))
    state.set_temporary_context("plugin.screen", TemporaryContextItem(title="页面", images=(new_image,)))
    request = adapter.encode_messages(append_transient_context(state))
    assert request[-1]["content"][-1]["image_url"]["url"] == new_image.url
    assert old_image.url not in json.dumps(state.to_checkpoint())
    state.remove_temporary_context("plugin.screen")
    assert adapter.encode_messages(append_transient_context(state)) == []
