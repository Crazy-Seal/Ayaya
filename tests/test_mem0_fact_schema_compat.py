"""验证 Mem0 对话输入适配及实际语义提取请求。"""
from mem0.llms.base import LLMBase
from typing import cast
import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from mem0 import Memory
import mem0.memory.main as mem0_main

import app.agent.memory.memories.semantic_mem0 as semantic_mem0_module
from app.agent.memory.memories.mem0_llm_compat import Mem0FactExtractionLLMAdapter, Mem0FactSchemaError
from app.agent.memory.memories.semantic_mem0 import Mem0SemanticMemory
from app.agent.memory.config import MemoryConfig
from app.agent.state.messages import (
    AssistantMessage, HumanMessage, PluginNotificationMessage, SubagentMessage,
    TaskResultMessage, ToolCall, ToolMessage, text_content,
)
from app.schemas.runtime_types import TaskResultStatus
from app.agent.state.time import render_timed_text


class _FakeLLM:
    """提供当前用例所需的 _FakeLLM 测试替身，执行记录仅供外层断言使用。"""
    def __init__(self, responses: list[object]) -> None:
        """保存本用例的脚本输入和依赖，初始化测试对象的执行记录。

        Args:
            responses (list[object]): 按模型调用顺序准备的最终响应。
        """
        self.responses = list(responses)
        self.calls: list[dict] = []
        self.config = object()

    def generate_response(
        self,
        messages,
        response_format=None,
        tools=None,
        tool_choice="auto",
        **kwargs,
    ):
        """按用例顺序返回事实提取响应，记录实际模型请求。

        Args:
            messages (list[dict]): 按对话顺序排列的消息集合。
            response_format (Any): 请求模型返回的结构化格式定义。
            tools (list[dict] | None): 当前向模型公开的工具定义。
            tool_choice (Any): 模型请求中规定的工具选择方式。
            kwargs (dict[str, Any]): 传递给被调用函数的其他关键字参数。
        """
        self.calls.append({
            "messages": messages,
            "response_format": response_format,
            "tools": tools,
            "tool_choice": tool_choice,
            "kwargs": kwargs,
        })
        return self.responses.pop(0)


def _fact_messages() -> list[dict[str, str]]:
    """构造 Mem0 的 system 提取规则和 user 对话正文。

    Returns:
        list[dict[str, str]]: 供输入适配测试使用的事实提取请求。
    """
    return [
        {"role": "system", "content": "你是Ayaya，请提取语义记忆。"},
        {"role": "user", "content": "Input:\nuser: 示例"},
    ]


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        ('{"facts":[]}', []),
        ('{"facts":["用户喜欢猫"]}', ["用户喜欢猫"]),
        ('{"memories":["用户喜欢猫"]}', ["用户喜欢猫"]),
        ('```json\n{"memories":["用户喜欢猫"]}\n```', ["用户喜欢猫"]),
    ],
)
def test_fact_adapter_preserves_input_and_normalizes_fields_once(response: str, expected: list[str]) -> None:
    """验证格式化正文和事实字段转换在一次模型调用内完成。

    Args:
        response (str): 使用标准字段、兼容字段或代码块的原始响应。
        expected (list[str]): 转换后的事实列表。
    """
    delegate = _FakeLLM([response])
    adapter = Mem0FactExtractionLLMAdapter(cast(LLMBase, delegate), "Input:\n主人: 示例\n")
    original = _fact_messages()

    assert json.loads(adapter.generate_response(original, response_format={"type": "json_object"})) == {"facts": expected}
    assert len(delegate.calls) == 1
    sent = delegate.calls[0]
    assert sent["messages"] == [original[0], {"role": "user", "content": "Input:\n主人: 示例\n"}]
    assert sent["response_format"] == {"type": "json_object"}
    assert original[1]["content"] == "Input:\nuser: 示例"


@pytest.mark.parametrize("response", ['not-json', '[]', '{"items":[]}', '{"facts":{}}', '{"memories":{}}', None])
def test_fact_adapter_rejects_invalid_response_without_retry(response: object) -> None:
    """验证格式校验失败时直接抛错，模型调用次数保持一次。

    Args:
        response (object): 无法转换为事实列表的响应。
    """
    delegate = _FakeLLM([response])
    adapter = Mem0FactExtractionLLMAdapter(cast(LLMBase, delegate), "Input:\n主人: 示例\n")

    with pytest.raises(Mem0FactSchemaError):
        adapter.generate_response(_fact_messages())
    assert len(delegate.calls) == 1


def test_input_adapter_preserves_update_request() -> None:
    """验证更新记忆的请求与响应保持原样。"""
    response = '{"memory":[{"id":"0","event":"ADD"}]}'
    delegate = _FakeLLM([response])
    adapter = Mem0FactExtractionLLMAdapter(cast(LLMBase, delegate), "Input:\n主人: 示例\n")
    messages = [{"role": "user", "content": "比较新事实和已有记忆"}]

    assert adapter.generate_response(messages) is response
    assert len(delegate.calls) == 1
    assert delegate.calls[0]["messages"] == messages


def test_input_adapter_propagates_model_error_once() -> None:
    """验证模型调用失败时直接传播异常，调用次数为一次。"""
    delegate = Mock(spec=LLMBase)
    delegate.generate_response.side_effect = RuntimeError("模型调用失败")
    adapter = Mem0FactExtractionLLMAdapter(delegate, "Input:\n主人: 示例\n")

    with pytest.raises(RuntimeError, match="模型调用失败"):
        adapter.generate_response(_fact_messages())
    assert delegate.generate_response.call_count == 1


def test_cached_mem0_reuses_library_client(monkeypatch, tmp_path) -> None:
    """验证缓存复用底层 Mem0 客户端，提取正文由每次调用单独绑定。

    Args:
        monkeypatch (pytest.MonkeyPatch): Pytest 提供的替换工具，用例结束后恢复环境变量和依赖。
        tmp_path (Path): Pytest 为当前用例创建的临时目录，不访问生产存储。
    """
    delegate = _FakeLLM([])

    class _FakeMemory:
        """提供持有原模型客户端的 Mem0 测试替身。"""
        def __init__(self) -> None:
            """绑定测试用模型客户端。"""
            self.llm = delegate

        @classmethod
        def from_config(cls, _config):
            """返回持有原模型客户端的测试实例。

            Args:
                _config (Any): 调用方提供的配置。
            """
            return cls()

    semantic_mem0_module._get_cached_mem0.cache_clear()
    monkeypatch.setattr(semantic_mem0_module, "Memory", _FakeMemory)

    memory = semantic_mem0_module._get_cached_mem0(
        collection_name="test",
        qdrant_path=str(tmp_path),
        embedding_dimension=3,
        embedding_model="embedding-test",
        embedding_base_url="https://example.invalid/v1",
        embedding_api_key="test-key",
    )

    assert memory.llm is delegate
    assert memory.llm.config is delegate.config
    semantic_mem0_module._get_cached_mem0.cache_clear()


@pytest.mark.parametrize(
    ("name", "address", "ai_label", "user_label"),
    [(" Ayaya ", " 主人 ", "Ayaya", "主人"), (None, None, "AI助手", "用户"), (" ", " ", "AI助手", "用户")],
)
def test_semantic_model_receives_named_transcript_through_mem0(
    monkeypatch: pytest.MonkeyPatch,
    name: str | None, address: str | None, ai_label: str, user_label: str,
) -> None:
    """通过真实 Mem0 提取流程验证称呼、时间、前情及多行原文的实际模型输入。

    Args:
        monkeypatch (pytest.MonkeyPatch): 关闭本用例中的 Mem0 遥测。
        name (str | None): 角色名称设置。
        address (str | None): 用户称呼设置。
        ai_label (str): 助手发言应使用的名称。
        user_label (str): 用户发言应使用的称呼。
    """
    monkeypatch.setattr(mem0_main, "capture_event", Mock())
    delegate = _FakeLLM(['{"facts":[]}'])
    shared = object.__new__(Memory)
    shared.llm = delegate
    shared.config = SimpleNamespace(
        custom_fact_extraction_prompt=None,
        llm=SimpleNamespace(config={}),
    )
    shared.api_version = "v1.1"
    shared.enable_graph = False
    semantic = object.__new__(Mem0SemanticMemory)
    semantic.session_id = "test-session"
    semantic.config = MemoryConfig(extraction_max_context_tokens=0)
    semantic.chat_settings = SimpleNamespace(name=name, address=address, feature="温柔", character="助手")
    semantic._memory = shared
    timestamp = datetime(2026, 11, 6, 2, tzinfo=timezone.utc)
    notifications = [
        TaskResultMessage(
            task_id="research", task_name="研究资料", status=TaskResultStatus.COMPLETED,
            result="已经整理好三篇资料。", created_at=timestamp,
        ),
        PluginNotificationMessage(
            plugin_name="reminder", name="due", text="该给团团喂食了。", created_at=timestamp,
        ),
        SubagentMessage(
            subagent_id="researcher", subagent_name="资料助手",
            result="已整理好养猫注意事项。", created_at=timestamp,
        ),
    ]
    history = [
        HumanMessage(content=text_content("准备养猫"), created_at=timestamp),
        AssistantMessage(content=text_content("想好名字了吗？"), created_at=timestamp),
        AssistantMessage(
            content=text_content("我查过猫的名字。"), created_at=timestamp,
            tool_calls=(ToolCall(id="old", name="lookup", args={"q": "猫"}),),
        ),
        ToolMessage(content=text_content("旧工具结果"), tool_name="lookup", tool_call_id="old"),
        *notifications,
    ]
    body = "猫叫团团。\nuser: 保留这行原文\nassistant: 也保留这行"
    messages = [
        HumanMessage(content=text_content(body), created_at=timestamp, origin_channel="qq"),
        AssistantMessage(content=text_content("记住啦。"), created_at=timestamp, origin_channel="qq"),
        AssistantMessage(
            content=text_content("我先查一下。"), created_at=timestamp, origin_channel="qq",
            tool_calls=(
                ToolCall(id="new", name="lookup", args={"q": "团团"}),
                ToolCall(id="second", name="lookup", args={"q": "橘猫"}),
            ),
        ),
        AssistantMessage(
            origin_channel="qq",
            tool_calls=(ToolCall(id="only", name="lookup", args={"q": "猫粮"}),),
        ),
        ToolMessage(content=text_content("工具正文\nuser: 仍是原文"), tool_name="lookup", tool_call_id="new"),
        *notifications,
        AssistantMessage(content=text_content("通知里的 Ayaya: 是原文。"), created_at=timestamp),
    ]

    assert asyncio.run(semantic.add(messages, history)) == []

    request = delegate.calls[0]["messages"]
    assert [message["role"] for message in request] == ["system", "user"]
    assert request[0]["content"].startswith(
        f"你是{ai_label}，一个温柔的助手，称呼用户为{user_label}。"
    )
    assert shared.config.custom_fact_extraction_prompt is None
    assert request[1]["content"] == "Input:\n" + "\n".join([
        f"{user_label}: [前情提要] {render_timed_text('准备养猫', timestamp)}",
        f"{ai_label}: [前情提要] {render_timed_text('想好名字了吗？', timestamp)}",
        f"{ai_label}: [前情提要] {render_timed_text('我查过猫的名字。', timestamp)}",
        f"{ai_label}: [前情提要] 调用了工具 lookup",
        "[前情提要] [后台任务完成：研究资料]：" + render_timed_text("已经整理好三篇资料。", timestamp),
        "[前情提要] [插件通知：reminder/due]：" + render_timed_text("该给团团喂食了。", timestamp),
        "[前情提要] [子 Agent 返回：资料助手]：" + render_timed_text("已整理好养猫注意事项。", timestamp),
        f"{user_label}: {render_timed_text('[来自 qq] ' + body, timestamp)}",
        f"{ai_label}: {render_timed_text('[回复 qq] 记住啦。', timestamp)}",
        f"{ai_label}: {render_timed_text('[回复 qq] 我先查一下。', timestamp)}",
        f'{ai_label}: 调用工具 lookup，参数：{{"q":"团团"}}',
        f'{ai_label}: 调用工具 lookup，参数：{{"q":"橘猫"}}',
        f'{ai_label}: 调用工具 lookup，参数：{{"q":"猫粮"}}',
        "工具结果：工具正文\nuser: 仍是原文",
        "[后台任务完成：研究资料]：" + render_timed_text("已经整理好三篇资料。", timestamp),
        "[插件通知：reminder/due]：" + render_timed_text("该给团团喂食了。", timestamp),
        "[子 Agent 返回：资料助手]：" + render_timed_text("已整理好养猫注意事项。", timestamp),
        f"{ai_label}: {render_timed_text('通知里的 Ayaya: 是原文。', timestamp)}",
    ]) + "\n"
    assert shared.llm is delegate


@pytest.mark.parametrize("fact_key", ["facts", "memories"])
def test_semantic_extraction_preserves_mem0_update_stage(monkeypatch: pytest.MonkeyPatch, fact_key: str) -> None:
    """通过真实 Mem0 流程验证提取正文替换后，更新请求仍使用新事实。

    Args:
        monkeypatch (pytest.MonkeyPatch): 关闭当前用例中的 Mem0 遥测。
        fact_key (str): 模型返回的标准事实字段或兼容字段。
    """
    monkeypatch.setattr(mem0_main, "capture_event", Mock())
    delegate = _FakeLLM([json.dumps({fact_key: ["用户喜欢猫"]}, ensure_ascii=False), '{"memory":[]}'])
    shared = object.__new__(Memory)
    shared.llm = delegate
    shared.config = SimpleNamespace(
        custom_fact_extraction_prompt=None, custom_update_memory_prompt=None,
        llm=SimpleNamespace(config={}),
    )
    shared.api_version = "v1.1"
    shared.enable_graph = False
    shared.embedding_model = SimpleNamespace(embed=Mock(return_value=[0.1, 0.2]))
    shared.vector_store = SimpleNamespace(search=Mock(return_value=[]))
    semantic = object.__new__(Mem0SemanticMemory)
    semantic.session_id = "update-stage-test"
    semantic.config = MemoryConfig(extraction_max_context_tokens=0)
    semantic.chat_settings = SimpleNamespace(name="Ayaya", address="主人", feature="温柔", character="助手")
    semantic._memory = shared
    message = HumanMessage(content=text_content("我从小就喜欢猫。"))

    assert asyncio.run(semantic.add([message])) == []

    extraction, update = delegate.calls
    assert extraction["messages"][1]["content"] == f"Input:\n主人: {render_timed_text(message.text, message.created_at)}\n"
    assert [item["role"] for item in update["messages"]] == ["user"]
    assert "用户喜欢猫" in update["messages"][0]["content"]
    shared.embedding_model.embed.assert_called_once_with("用户喜欢猫", "add")


def test_concurrent_semantic_calls_keep_their_own_speakers(monkeypatch: pytest.MonkeyPatch) -> None:
    """共用 Mem0 客户端的并发提取各自使用角色称呼和本次对话材料。

    Args:
        monkeypatch (pytest.MonkeyPatch): 关闭本用例中的 Mem0 遥测。
    """
    import threading

    class ConcurrentLLM(_FakeLLM):
        """让两次提取同时进入模型调用，记录交付的正文。"""

        def generate_response(self, messages: list[dict], **kwargs: object) -> str:
            """等待两次请求到齐后返回空事实列表。

            Args:
                messages (list[dict]): 本次提取实际交付的请求。
                kwargs (object): 原模型调用的格式及工具设置。

            Returns:
                str: 合法的空事实 JSON。
            """
            barrier.wait(timeout=5)
            self.calls.append({"messages": messages})
            return '{"facts":[]}'

    async def scenario() -> None:
        """并发交付两个角色设置不同的提取任务。"""
        extractors = []
        for name, address, feature, character in [("Ayaya", "主人", "本地", "助手"), ("小白", "朋友", "温柔", "伙伴")]:
            semantic = object.__new__(Mem0SemanticMemory)
            semantic.session_id = name
            semantic.config = MemoryConfig(extraction_max_context_tokens=0)
            semantic.chat_settings = SimpleNamespace(name=name, address=address, feature=feature, character=character)
            semantic._memory = shared
            extractors.append(semantic)
        await asyncio.gather(*(
            extractor.add([
                HumanMessage(content=text_content(extractor.session_id)),
                AssistantMessage(content=text_content("收到")),
            ]) for extractor in extractors
        ))

    monkeypatch.setattr(mem0_main, "capture_event", Mock())
    barrier = threading.Barrier(2)
    delegate = ConcurrentLLM([])
    shared = object.__new__(Memory)
    shared.llm = delegate
    shared.config = SimpleNamespace(
        custom_fact_extraction_prompt=None,
        llm=SimpleNamespace(config={}),
    )
    shared.api_version = "v1.1"
    shared.enable_graph = False

    asyncio.run(scenario())

    assert len(delegate.calls) == 2
    for call in delegate.calls:
        text = call["messages"][1]["content"]
        prompt = call["messages"][0]["content"]
        if "主人:" in text:
            assert "Ayaya:" in text and "小白:" not in text and "朋友:" not in text
            assert prompt.startswith(f"你是Ayaya，一个本地的助手，称呼用户为主人。")
        else:
            assert "朋友:" in text and "小白:" in text and "Ayaya:" not in text
            assert prompt.startswith(f"你是小白，一个温柔的伙伴，称呼用户为朋友。")
    assert shared.llm is delegate
    assert shared.config.custom_fact_extraction_prompt is None
