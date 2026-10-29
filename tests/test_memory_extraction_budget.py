"""通过真实记忆入口检查预算归属、实际发送材料和共享消息的保护。"""
from app.agent.state.messages import text_content
from tests.model_capture import capture_input

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.agent.memory.config import MemoryConfig
from app.agent.memory.manager import MemoryManager
from app.agent.memory.memories.episodic import EpisodicMemory
from app.agent.memory.memories.semantic_mem0 import Mem0SemanticMemory
from app.agent.state.messages import AssistantMessage, BaseMessage, HumanMessage, ToolCall, ToolMessage


class RecordingLLM:
    """替换外部情景模型，记录实际请求并返回不需要存储的空记忆。

    Attributes:
        requests (list[list[dict]]): 已发送的模型消息。
    """
    def __init__(self) -> None:
        """准备本测试独立的请求记录。"""
        self.requests: list[list[dict]] = []

    async def ainvoke_structured(self, *, messages: list[dict], schema: dict) -> dict:
        """记录情景提取器实际发送的材料，避免调用外部模型。

        Args:
            messages (list[dict]): 已完成预算处理的模型消息。
            schema (dict): 情景提取结果结构。

        Returns:
            dict: 符合结构要求的空记忆结果。
        """
        self.requests.append(deepcopy(capture_input(messages)))
        return {"memories": []}


class RecordingMem0:
    """替换外部 Mem0 提取，记录最终材料并提供一个可汇总的结果编号。

    Attributes:
        requests (list[list[dict]]): 已发送的 Mem0 消息列表。
    """
    def __init__(self) -> None:
        """准备本测试独立的请求记录。"""
        self.requests: list[list[dict]] = []

    def add(self, messages: list[dict], *, agent_id: str) -> dict:
        """在线程调用中记录转换后的材料，不初始化外部向量库。

        Args:
            messages (list[dict]): 历史在前、当前材料在后的提取输入。
            agent_id (str): 当前记忆会话编号。

        Returns:
            dict: 含一条记忆编号的 Mem0 结果。
        """
        self.requests.append(deepcopy(capture_input(messages)))
        return {"results": [{"id": "semantic-1"}]}


def make_extractors(limit: int | None) -> tuple[EpisodicMemory, Mem0SemanticMemory]:
    """装配真实提取器的材料逻辑，仅替换外部模型和 Mem0 客户端。

    Args:
        limit (int | None): 本测试使用的现有提取预算配置。

    Returns:
        tuple[EpisodicMemory, Mem0SemanticMemory]: 不初始化外部存储的两个提取器。
    """
    config = MemoryConfig(extraction_max_context_tokens=limit)
    episodic = object.__new__(EpisodicMemory)
    episodic.session_id = "s"
    episodic.config = config
    episodic.chat_settings = SimpleNamespace(
        name="Ayaya", address="用户", feature="本地", character="助手", characteristic="",
    )
    episodic.llm = RecordingLLM()
    semantic = object.__new__(Mem0SemanticMemory)
    semantic.session_id = "s"
    semantic.config = config
    semantic._memory = RecordingMem0()
    return episodic, semantic


def extraction_messages() -> tuple[list[BaseMessage], list[BaseMessage]]:
    """构造带时间、渠道、工具参数和长结果的当前对话及历史。

    Returns:
        tuple[list[BaseMessage], list[BaseMessage]]: 当前材料和只作前情提要的历史。
    """
    timestamp = datetime(2026, 10, 10, 2, 0, tzinfo=timezone.utc)
    current = [
        HumanMessage(content=text_content("当前偏好"), origin_channel="qq", created_at=timestamp),
        AssistantMessage(tool_calls=(ToolCall(id="call", name="read_file", args={"path": "a.py"}),)),
        ToolMessage(content=text_content("头" * 1_200 + "中" * 5_000 + "尾" * 1_200),
                    tool_name="read_file", tool_call_id="call"),
    ]
    history = [
        HumanMessage(content=text_content("历史偏好" + "史" * 1_500), created_at=timestamp),
        AssistantMessage(tool_calls=(ToolCall(id="old", name="read_file", args={"path": "old.py"}),)),
        ToolMessage(content=text_content("历史工具结果不应进入材料"), tool_name="read_file", tool_call_id="old"),
    ]
    return current, history


@pytest.mark.parametrize("limit", [None, 100_000, 1])
def test_extractors_budget_actual_materials_without_mutating_shared_messages(limit: int | None) -> None:
    """验证无预算、预算充足及超限时，两种提取器实际发送的内容和格式。

    Args:
        limit (int | None): 不限制、足够大或触发压缩的预算。
    """
    async def scenario() -> None:
        """通过两个真实 add 入口记录外部请求，核对压缩和原始材料。"""
        episodic, semantic = make_extractors(limit)
        messages, history = extraction_messages()
        before = deepcopy((messages, history))
        # 1. 两种提取器共用同一批原始消息，压缩只应影响各自的最终请求。
        assert await episodic.add(messages, history) == []
        assert await semantic.add(messages, history) == ["semantic-1"]
        prompt = episodic.llm.requests[0][0]["content"]
        converted = semantic._memory.requests[0]
        semantic_text = "\n".join(item["content"] for item in converted)
        for text in (prompt, semantic_text):
            assert "当前偏好" in text and "qq" in text and "发送时间" in text
            assert '参数：{"path":"a.py"}' in text
            assert history[0].text in text
            assert "历史工具结果不应进入材料" not in text
            if limit == 1:
                assert "头" * 800 in text and "尾" * 200 in text
                assert "中" * 100 not in text and messages[-1].text not in text
            else:
                assert messages[-1].text in text
        # 2. Mem0 仍将带标记历史放在当前材料前，两个提取器不改变共享消息值。
        assert converted[0]["content"].startswith("[前情提要]")
        assert (messages, history) == before

    asyncio.run(scenario())


def test_manager_respects_each_extractor_budget_and_only_coordinates_results() -> None:
    """验证协调者不需要请求构造知识，两种提取器按各自预算处理同一材料。"""
    async def scenario() -> None:
        """让情景提取压缩、语义提取保持全文，通过真实协调入口验证互不影响。"""
        episodic, semantic = make_extractors(1)
        semantic.config = MemoryConfig(extraction_max_context_tokens=100_000)
        manager = object.__new__(MemoryManager)
        manager.episodic_memory = episodic
        manager.semantic_memory = semantic
        messages, history = extraction_messages()
        # 1. 不装配协调者的预算或提示词配置，实际输入处理必须由提取器独立完成。
        assert await manager.add(messages, history) == {"episodic": 0, "semantic": 1}
        prompt = episodic.llm.requests[0][0]["content"]
        semantic_text = "\n".join(item["content"] for item in semantic._memory.requests[0])
        assert messages[-1].text not in prompt
        assert messages[-1].text in semantic_text

    asyncio.run(scenario())


@pytest.mark.parametrize("disabled", ["episodic", "semantic"])
def test_disabled_extractor_does_not_prepare_or_send_material(disabled: str) -> None:
    """验证本次禁用的提取器不构造材料，也不影响另一项结果。

    Args:
        disabled (str): 本次禁用的记忆类别。
    """
    async def scenario() -> None:
        """让禁用提取器缺少材料配置，确认协调者仍只调用启用的入口。"""
        episodic, semantic = make_extractors(1)
        if disabled == "episodic":
            del episodic.config
            del episodic.chat_settings
        else:
            del semantic.config
        manager = object.__new__(MemoryManager)
        manager.episodic_memory = episodic
        manager.semantic_memory = semantic
        messages, history = extraction_messages()
        results = await manager.add(
            messages, history, enable_episodic=disabled != "episodic", enable_semantic=disabled != "semantic",
        )
        assert results == {"episodic": 0, "semantic": 1 if disabled == "episodic" else 0}
        assert len(episodic.llm.requests) == (0 if disabled == "episodic" else 1)
        assert len(semantic._memory.requests) == (0 if disabled == "semantic" else 1)

    asyncio.run(scenario())


def test_material_preparation_failure_does_not_prevent_other_extraction() -> None:
    """一种提取器构造材料失败时，另一种仍完成提取，异常摘要保留其数量。"""
    async def scenario() -> None:
        """缺少情景角色配置时只使情景提取失败，语义提取仍使用真实材料逻辑。"""
        episodic, semantic = make_extractors(1)
        del episodic.chat_settings
        manager = object.__new__(MemoryManager)
        manager.episodic_memory = episodic
        manager.semantic_memory = semantic
        messages, history = extraction_messages()
        with pytest.raises(RuntimeError, match="部分失败.*语义记忆：处理 1 条"):
            await manager.add(messages, history)
        assert episodic.llm.requests == []
        assert len(semantic._memory.requests) == 1

    asyncio.run(scenario())


def test_episodic_budget_includes_prompt_instructions_not_only_dialogue() -> None:
    """验证情景预算包括角色和提取要求，短工具结果也可因完整提示词超限被压缩。"""
    async def scenario() -> None:
        """让角色设定超过预算，核对实际发送的当前工具结果已按策略缩短。"""
        episodic, _semantic = make_extractors(1_500)
        episodic.chat_settings.characteristic = "设定" * 2_000
        content = "头" * 800 + "中" * 100 + "尾" * 200
        assert await episodic.add([ToolMessage(content=text_content(content), tool_name="read_file", tool_call_id="call")]) == []
        prompt = episodic.llm.requests[0][0]["content"]
        assert episodic.chat_settings.characteristic in prompt
        assert content not in prompt and "中" * 100 not in prompt

    asyncio.run(scenario())
