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
    def __init__(self, responses: list[object]) -> None:
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
        self.calls.append({
            "messages": messages,
            "response_format": response_format,
            "tools": tools,
            "tool_choice": tool_choice,
            "kwargs": kwargs,
        })
        return self.responses.pop(0)


def _fact_messages() -> list[dict]:
    return [
        {
            "role": "system",
            "content": f"{MEM0_FACT_EXTRACTION_MARKER}\n提取语义记忆。",
        },
        {"role": "user", "content": "Input:\nuser: 示例"},
    ]


def test_fact_adapter_normalizes_memories_alias_without_retry() -> None:
    delegate = _FakeLLM(['{"memories":["用户喜欢卡牌游戏"]}'])
    adapter = Mem0FactExtractionLLMAdapter(delegate)

    response = adapter.generate_response(
        _fact_messages(),
        response_format={"type": "json_object"},
    )

    assert json.loads(response) == {"facts": ["用户喜欢卡牌游戏"]}
    assert len(delegate.calls) == 1


def test_fact_adapter_preserves_standard_facts_contract() -> None:
    delegate = _FakeLLM(['{"facts":[]}'])
    adapter = Mem0FactExtractionLLMAdapter(delegate)

    response = adapter.generate_response(_fact_messages())

    assert json.loads(response) == {"facts": []}
    assert len(delegate.calls) == 1


def test_fact_adapter_accepts_memories_alias_in_json_code_block() -> None:
    delegate = _FakeLLM([
        '```json\n{"memories":["用户喜欢卡牌游戏"]}\n```',
    ])
    adapter = Mem0FactExtractionLLMAdapter(delegate)

    response = adapter.generate_response(_fact_messages())

    assert json.loads(response) == {"facts": ["用户喜欢卡牌游戏"]}


def test_fact_adapter_does_not_modify_update_stage_payload() -> None:
    raw = '{"memory":[{"id":"0","text":"内容","event":"ADD"}]}'
    delegate = _FakeLLM([raw])
    adapter = Mem0FactExtractionLLMAdapter(delegate)

    response = adapter.generate_response([
        {"role": "user", "content": "比较新事实和已有记忆"},
    ])

    assert response == raw
    assert len(delegate.calls) == 1


def test_fact_adapter_retries_unknown_schema_once() -> None:
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
    delegate = _FakeLLM(responses)
    adapter = Mem0FactExtractionLLMAdapter(delegate)

    with pytest.raises(Mem0FactSchemaError, match="两次响应均不符合"):
        adapter.generate_response(_fact_messages())

    assert len(delegate.calls) == 2


def test_fact_prompt_declares_exact_contract_and_marker() -> None:
    assert MEM0_FACT_EXTRACTION_MARKER in MEM0_CUSTOM_INSTRUCTIONS
    assert '{"facts": ["记忆1", "记忆2"]}' in MEM0_CUSTOM_INSTRUCTIONS
    assert '{"facts": []}' in MEM0_CUSTOM_INSTRUCTIONS


def test_cached_mem0_wraps_library_llm(monkeypatch, tmp_path) -> None:
    delegate = _FakeLLM([])

    class _FakeMemory:
        def __init__(self) -> None:
            self.llm = delegate

        @classmethod
        def from_config(cls, _config):
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
