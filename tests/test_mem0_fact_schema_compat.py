import json

import pytest

import app.agent.memory.memories.semantic_mem0 as semantic_mem0_module
from app.agent.memory.memories.mem0_llm_compat import (
    MEM0_FACT_EXTRACTION_MARKER,
    Mem0FactExtractionLLMAdapter,
    Mem0FactSchemaError,
)
from app.agent.memory.memories.semantic_mem0 import MEM0_CUSTOM_INSTRUCTIONS


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
        """按用例顺序返回事实提取响应，记录请求以验证结构修复次数。

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


def _fact_messages() -> list[dict]:
    """构造带 Mem0 事实提取标记的模型输入。

    Returns:
        list[dict]: 供外层用例断言使用的预设结果。
    """
    return [
        {
            "role": "system",
            "content": f"{MEM0_FACT_EXTRACTION_MARKER}\n提取语义记忆。",
        },
        {"role": "user", "content": "Input:\nuser: 示例"},
    ]


def test_fact_adapter_normalizes_memories_alias_without_retry() -> None:
    """验证memories 别名规范化为 facts 且不重试。"""
    delegate = _FakeLLM(['{"memories":["用户喜欢卡牌游戏"]}'])
    adapter = Mem0FactExtractionLLMAdapter(delegate)

    response = adapter.generate_response(
        _fact_messages(),
        response_format={"type": "json_object"},
    )

    assert json.loads(response) == {"facts": ["用户喜欢卡牌游戏"]}
    assert len(delegate.calls) == 1


def test_fact_adapter_preserves_standard_facts_contract() -> None:
    """验证标准 facts 结构保持不变。"""
    delegate = _FakeLLM(['{"facts":[]}'])
    adapter = Mem0FactExtractionLLMAdapter(delegate)

    response = adapter.generate_response(_fact_messages())

    assert json.loads(response) == {"facts": []}
    assert len(delegate.calls) == 1


def test_fact_adapter_accepts_memories_alias_in_json_code_block() -> None:
    """验证代码块内 memories 别名仍可规范化。"""
    delegate = _FakeLLM([
        '```json\n{"memories":["用户喜欢卡牌游戏"]}\n```',
    ])
    adapter = Mem0FactExtractionLLMAdapter(delegate)

    response = adapter.generate_response(_fact_messages())

    assert json.loads(response) == {"facts": ["用户喜欢卡牌游戏"]}


def test_fact_adapter_does_not_modify_update_stage_payload() -> None:
    """验证非事实提取阶段响应不被修改。"""
    raw = '{"memory":[{"id":"0","text":"内容","event":"ADD"}]}'
    delegate = _FakeLLM([raw])
    adapter = Mem0FactExtractionLLMAdapter(delegate)

    response = adapter.generate_response([
        {"role": "user", "content": "比较新事实和已有记忆"},
    ])

    assert response == raw
    assert len(delegate.calls) == 1


def test_fact_adapter_retries_unknown_schema_once() -> None:
    """验证未知事实结构只修复重试一次。"""
    delegate = _FakeLLM([
        '{"items":["用户喜欢卡牌游戏"]}',
        '{"facts":["用户喜欢卡牌游戏"]}',
    ])
    adapter = Mem0FactExtractionLLMAdapter(delegate)
    original_messages = _fact_messages()

    response = adapter.generate_response(original_messages)

    assert json.loads(response) == {"facts": ["用户喜欢卡牌游戏"]}
    assert len(delegate.calls) == 2
    assert len(delegate.calls[1]["messages"]) == len(original_messages) + 1
    assert len(original_messages) == 2


def test_fact_adapter_raises_after_one_failed_repair() -> None:
    """验证一次修复仍失败后明确抛错。"""
    delegate = _FakeLLM(['{"items":[]}', '{"result":[]}'])
    adapter = Mem0FactExtractionLLMAdapter(delegate)

    with pytest.raises(Mem0FactSchemaError, match="两次响应均不符合"):
        adapter.generate_response(_fact_messages())

    assert len(delegate.calls) == 2


@pytest.mark.parametrize(
    "responses",
    [
        ["not-json", "still-not-json"],
        ["[]", "[]"],
        ['{"facts":{}}', '{"facts":{}}'],
        ['{"memories":{}}', '{"memories":{}}'],
        [{"facts": []}, {"facts": []}],
    ],
)
def test_fact_adapter_rejects_invalid_shapes_after_one_retry(
    responses: list[object],
) -> None:
    """验证无效事实结构在一次重试后被拒绝。

    Args:
        responses (list[object]): 按模型调用顺序准备的最终响应。
    """
    delegate = _FakeLLM(responses)
    adapter = Mem0FactExtractionLLMAdapter(delegate)

    with pytest.raises(Mem0FactSchemaError, match="两次响应均不符合"):
        adapter.generate_response(_fact_messages())

    assert len(delegate.calls) == 2


def test_fact_prompt_declares_exact_contract_and_marker() -> None:
    """验证事实提示词声明准确结构和识别标记。"""
    assert MEM0_FACT_EXTRACTION_MARKER in MEM0_CUSTOM_INSTRUCTIONS
    assert '{"facts": ["记忆1", "记忆2"]}' in MEM0_CUSTOM_INSTRUCTIONS
    assert '{"facts": []}' in MEM0_CUSTOM_INSTRUCTIONS


def test_cached_mem0_wraps_library_llm(monkeypatch, tmp_path) -> None:
    """验证缓存 Mem0 实例也包装库内模型客户端。

    Args:
        monkeypatch (pytest.MonkeyPatch): Pytest 提供的替换工具，用例结束后恢复环境变量和依赖。
        tmp_path (Path): Pytest 为当前用例创建的临时目录，不访问生产存储。
    """
    delegate = _FakeLLM([])

    class _FakeMemory:
        """提供当前用例所需的 _FakeMemory 测试替身，执行记录仅供外层断言使用。

        该定义仅用于外层用例：验证缓存 Mem0 实例也包装库内模型客户端。
        """
        def __init__(self) -> None:
            """保存本用例的脚本输入和依赖，初始化测试对象的执行记录。

            该定义仅用于外层用例：验证缓存 Mem0 实例也包装库内模型客户端。
            """
            self.llm = delegate

        @classmethod
        def from_config(cls, _config):
            """记录 Mem0 初始化配置并返回用例提供的实例。

            该定义仅用于外层用例：验证缓存 Mem0 实例也包装库内模型客户端。

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

    assert isinstance(memory.llm, Mem0FactExtractionLLMAdapter)
    assert memory.llm.config is delegate.config
    semantic_mem0_module._get_cached_mem0.cache_clear()
