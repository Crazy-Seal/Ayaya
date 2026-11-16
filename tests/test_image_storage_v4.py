"""通过会话保存和模型入口验证文件引用图片的生命周期。"""

import asyncio
import json
from pathlib import Path
import sqlite3

import pytest

from app.agent.state import AgentState, TemporaryContextItem
from app.agent.state.manager import SessionStateManager
from app.agent.state.messages import ImageContent, ToolCall
from app.crud.sqlite_factory import SqliteFactory
from app.schemas.agent_storage import AgentStorageScope
from app.schemas.message_types import MediaKind
from app.schemas.runtime_types import CheckpointType
from tests.model_capture import result_parts


def test_image_cache_tracks_file_changes_and_evicts_old_entries(tmp_path: Path, monkeypatch) -> None:
    """重复读取使用缓存，替换、删除和超过容量后重新读取磁盘。

    Args:
        tmp_path (Path): 隔离图片目录。
        monkeypatch (pytest.MonkeyPatch): 记录图片文件读取次数。
    """
    from app.agent.utils.domain import images

    monkeypatch.setattr(images, "get_images_dir", lambda: tmp_path)
    path = tmp_path / "cache.png"
    path.write_bytes(b"first")
    original_read = Path.read_bytes
    reads: list[Path] = []

    def read_bytes(file: Path) -> bytes:
        """记录本用例图片读取，实际内容仍由文件系统提供。

        Args:
            file (Path): 当前读取的图片文件。
        Returns:
            bytes: 磁盘保存的图片字节。
        """
        reads.append(file)
        return original_read(file)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    assert images.load_image_data_url("cache.png") == "data:image/png;base64,Zmlyc3Q="
    images.load_image_data_url("cache.png")
    assert reads.count(path) == 1
    path.write_bytes(b"second-version")
    assert images.load_image_data_url("cache.png") == "data:image/png;base64,c2Vjb25kLXZlcnNpb24="
    for index in range(32):
        (tmp_path / f"extra-{index}.png").write_bytes(b"x")
        images.load_image_data_url(f"extra-{index}.png")
    images.load_image_data_url("cache.png")
    assert reads.count(path) == 3
    path.unlink()
    with pytest.raises(FileNotFoundError):
        images.load_image_data_url("cache.png")
    with pytest.raises(ValueError):
        images.load_image_data_url("../outside.png")


def test_model_request_resolves_saved_image_without_modifying_message(tmp_path: Path, monkeypatch) -> None:
    """模型客户端读取文件引用发送图片，保留历史消息及其文字投影。

    Args:
        tmp_path (Path): 隔离图片目录。
        monkeypatch (pytest.MonkeyPatch): 替换文件目录和 SDK 网络请求。
    """
    from types import SimpleNamespace
    from app.agent.models.llm_client import LLMClient, LLMConfig
    from app.agent.models.contracts import ProjectedMessage
    from app.agent.state.messages import HumanMessage, TextContent, PluginNotificationMessage
    from app.agent.utils.domain import images

    monkeypatch.setattr(images, "get_images_dir", lambda: tmp_path)
    (tmp_path / "example.png").write_bytes(b"example")
    image = ImageContent(storage_ref="example.png", detail="low")
    message = HumanMessage(content=(TextContent("原文"), image))
    requests: list[dict] = []

    async def create(**kwargs):
        """记录 SDK 请求，在本机返回测试模型正文。

        Args:
            kwargs (dict): 模型适配器交付给 SDK 的请求字段。
        """
        requests.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(
            finish_reason="stop", message=SimpleNamespace(content="收到"))])

    async def scenario() -> None:
        """通过公开模型接口发起一次请求并关闭连接。"""
        client = LLMClient(LLMConfig("test", "test"))
        monkeypatch.setattr(client._adapter._client.chat.completions, "create", create)
        try:
            notice = PluginNotificationMessage(plugin_name="test", name="notice", text="通知原文")
            await client.ainvoke((ProjectedMessage(notice, "投影正文"), message))
        finally:
            await client.close()

    asyncio.run(scenario())
    assert requests[0]["messages"][1]["content"][-1] == {
        "type": "image_url", "image_url": {"url": "data:image/png;base64,ZXhhbXBsZQ==", "detail": "low"}}
    assert "投影正文" in str(requests[0]["messages"])
    assert message.content == (TextContent("原文"), image)
    assert image.url is None


def test_runtime_refuses_unmigrated_database_without_changing_version(tmp_path: Path) -> None:
    """运行时读取 v3 库时要求离线迁移，保持旧数据库版本与记录。

    Args:
        tmp_path (Path): 隔离旧数据库目录。
    """
    path = tmp_path / "old.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE checkpoints(id INTEGER PRIMARY KEY,state_json TEXT)")
        db.execute("PRAGMA user_version=3")

    async def scenario() -> None:
        """通过会话加载入口验证旧库拒绝及连接清理。"""
        manager = SessionStateManager("old", SqliteFactory(path))
        try:
            with pytest.raises(RuntimeError, match="离线迁移"):
                await manager.load()
        finally:
            await manager.close()

    asyncio.run(scenario())
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3


def test_compression_keeps_original_image_in_visible_history(tmp_path: Path) -> None:
    """上下文工具结果改成压缩文字后，原始历史仍能通过图片引用展示。

    Args:
        tmp_path (Path): 隔离会话数据库目录。
    """
    from dataclasses import replace
    from app.agent.state.messages import TextContent
    from app.crud.chat_history_dao import ChatHistoryDao

    async def scenario() -> None:
        """先保存原始观察，再提交压缩后的上下文，并读取公开历史。"""
        factory = SqliteFactory(tmp_path / "state.sqlite3")
        manager = SessionStateManager("compressed", factory)
        state = AgentState.create_new("compressed")
        call = ToolCall("call", "read_image", {})
        state.record_assistant_step("", (call,))
        state.record_tool_result(call, result_parts("观察", ("data:image/png;base64,AA==",), "tool"))
        try:
            await manager.save_progress(state)
            image = state.messages[-1].content[-1]
            state.replace_message(replace(state.messages[-1], content=(TextContent("已压缩"),)))
            await manager.save_progress(state)
            restored = await manager.load()
            assert restored.messages[-1].text == "已压缩"
            history = await ChatHistoryDao(factory).list_chat_history_async("compressed")
            assert next(item for item in history if item.get("images"))["images"] == [image.storage_ref]
        finally:
            await manager.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("method", ["estimate", "stream", "async", "structured", "sync"])
@pytest.mark.parametrize("missing", [False, True])
def test_all_model_entries_use_same_image_projection(tmp_path: Path, monkeypatch, method: str, missing: bool) -> None:
    """全部模型入口使用相同图片解析和缺图投影，网络请求保持真实 SDK 编码。

    Args:
        tmp_path (Path): 隔离图片目录。
        monkeypatch (pytest.MonkeyPatch): 替换 HTTP 请求以避免访问模型服务。
        method (str): 本次验证的公开模型入口。
        missing (bool): 图片文件是否缺失。
    """
    import httpx
    from app.agent.models.llm_client import LLMClient, LLMConfig
    from app.agent.models.openai_chat import OpenAIChatCompletionsAdapter
    from app.agent.state.messages import HumanMessage, TextContent
    from app.agent.utils.domain import images

    monkeypatch.setattr(images, "get_images_dir", lambda: tmp_path)
    if not missing:
        (tmp_path / "model.jpg").write_bytes(b"xyz")
    original = HumanMessage(content=(TextContent("查看"), ImageContent(storage_ref="model.jpg")))
    requests: list[dict] = []

    def respond(request: httpx.Request) -> httpx.Response:
        """记录模型 HTTP 请求并生成普通或 SSE 响应。

        Args:
            request (httpx.Request): SDK 编码的模型请求。
        Returns:
            httpx.Response: 本用例的本机模型响应。
        """
        payload = json.loads(request.content)
        requests.append(payload)
        choice = {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": '{"ok":true}'}}
        if payload.get("stream"):
            chunk = {"id": "test", "object": "chat.completion.chunk", "created": 1, "model": "test",
                     "choices": [{"index": 0, "delta": {"content": "好"}, "finish_reason": "stop"}]}
            return httpx.Response(200, request=request, headers={"content-type": "text/event-stream"},
                                  content="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n")
        return httpx.Response(200, request=request, json={"id": "test", "object": "chat.completion",
            "created": 1, "model": "test", "choices": [choice]})

    async def send_async(client, request: httpx.Request, **kwargs) -> httpx.Response:
        """让异步 SDK 使用本机响应。

        Args:
            client (httpx.AsyncClient): 请求所属客户端。
            request (httpx.Request): 本次模型请求。
            kwargs (dict): HTTP 传输选项。
        Returns:
            httpx.Response: 测试模型响应。
        """
        return respond(request)

    def send_sync(client, request: httpx.Request, **kwargs) -> httpx.Response:
        """让同步 SDK 使用本机响应。

        Args:
            client (httpx.Client): 请求所属客户端。
            request (httpx.Request): 本次模型请求。
            kwargs (dict): HTTP 传输选项。
        Returns:
            httpx.Response: 测试模型响应。
        """
        return respond(request)

    monkeypatch.setattr(httpx.AsyncClient, "send", send_async)
    monkeypatch.setattr(httpx.Client, "send", send_sync)
    expected = TextContent("[图片不可用：model.jpg]") if missing else ImageContent("data:image/jpeg;base64,eHl6")
    expected_message = HumanMessage(content=(TextContent("查看"), expected))

    async def scenario() -> None:
        """调用指定公开接口，并在结束后释放 SDK 资源。"""
        async with LLMClient(LLMConfig("test", "test", base_url="http://test/v1")) as client:
            if method == "estimate":
                adapter = OpenAIChatCompletionsAdapter(client.config, sdk_client=client._adapter._client)
                assert client.estimate_input((original,)) == adapter.estimate_input((expected_message,))
            elif method == "stream":
                async for _ in client.astream((original,)):
                    pass
            elif method == "async":
                await client.ainvoke((original,))
            elif method == "structured":
                await client.ainvoke_structured((original,), {"type": "object", "properties": {"ok": {"type": "boolean"}}})
            else:
                client.invoke((original,))

    asyncio.run(scenario())
    if method != "estimate":
        expected_content = expected_message.text if missing else [part.to_dict() if isinstance(part, TextContent)
            else {"type": "image_url", "image_url": {"url": part.url}} for part in expected_message.content]
        assert requests[0]["messages"][0]["content"] == expected_content
    assert original.content[-1].url is None


@pytest.mark.parametrize("scope", list(AgentStorageScope))
def test_save_and_restore_all_local_images_as_references(tmp_path: Path, scope: AgentStorageScope) -> None:
    """父子会话保存附件、各类工具图和临时背景后，恢复结果只含文件引用。

    Args:
        tmp_path (Path): 隔离数据库目录。
        scope (AgentStorageScope): 当前验证主 Agent 或子 Agent 的存储入口。
    """
    async def scenario() -> None:
        """保存同一轮的全部图片，并通过真实恢复及历史数据核对内容。"""
        path = tmp_path / "state.sqlite3"
        manager = SessionStateManager("images", SqliteFactory(path), storage_scope=scope)
        state = AgentState.create_new("images")
        image = "data:image/png;base64,AA=="
        state.add_user_message("附件", image_urls=(image,))
        for kind in (MediaKind.TOOL, MediaKind.MCP):
            call = ToolCall(f"call-{kind}", "draw", {})
            state.record_assistant_step("", (call,))
            state.record_tool_result(call, result_parts("结果", (image,), kind))
        state.set_temporary_context("plugin.screen", TemporaryContextItem(
            title="环境", images=(ImageContent(image, media_kind=MediaKind.ENVIRONMENT),)))
        try:
            await manager.save(state, checkpoint_type=CheckpointType.INTERMEDIATE)
            restored = await manager.load()
            assert restored is not None
            pictures = [part for message in restored.messages for part in getattr(message, "content", ())
                        if isinstance(part, ImageContent)]
            pictures.extend(restored.temporary_context["plugin.screen"].images)
            assert len(pictures) == 4
            assert all(part.url is None and part.storage_ref for part in pictures)
            with sqlite3.connect(path) as db:
                checkpoint = db.execute(f"SELECT state_json FROM {scope.checkpoints_table}").fetchone()[0]
                history = db.execute(f"SELECT message_json FROM {scope.messages_table}").fetchall()
            assert "base64" not in checkpoint
            assert all("base64" not in row[0] for row in history)
            assert json.loads(checkpoint)["schema_version"] == 4
        finally:
            await manager.close()

    asyncio.run(scenario())
