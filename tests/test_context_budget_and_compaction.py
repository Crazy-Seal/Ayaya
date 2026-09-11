import asyncio
import json
import pytest
from pydantic import ValidationError
from app.agent.context import ContextStrategyConfig, RuntimeContext
from app.agent.context._budget import (
    ContextBudgetService,
    TokenAssessment,
)
from app.agent.context._compaction import (
    COMPRESSED_TOOL_RESULT,
    ContextCompressionPolicy,
)
from app.agent.context._projection import append_transient_context
from app.agent.state.messages import (
    CONTEXT_SUMMARY_MESSAGE_NAME,
    MCP_TOOL_IMAGE_MESSAGE_NAME,
    SCREENSHOT_COMPRESSED_NAME,
    SCREENSHOT_MESSAGE_NAME,
)
from app.agent.context.coordinator import (
    CONTEXT_SUMMARY_SCHEMA,
    SUMMARY_SYSTEM_PROMPT,
    ContextCompactionCoordinator,
    ContextOverflowError,
    ContextSummary,
    ModelInputCoordinator,
)
from app.agent.state import AgentState, TokenBaseline
from app.agent.tools.control_screen import control_screen
from app.agent.tools.control_screen_with_vlm import control_screen_with_vlm
from app.agent.tools.screenshot import screenshot
from datetime import datetime, timezone
from types import SimpleNamespace


def _summary_settings(*, address: str = "用户") -> SimpleNamespace:
    return SimpleNamespace(
        name="Ayaya",
        feature="",
        character="助手",
        address=address,
        characteristic="",
    )


def _tool_call(message_id: str, tool_call_id: str) -> dict:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": tool_call_id,
            "type": "function",
            "function": {"name": "read_file", "arguments": "{}"},
        }],
        "_message_id": message_id,
        "_created_at": "2026-08-29T00:00:00+00:00",
    }


def _tool_result(
    index: int,
    *,
    compressible: bool = True,
    content: str | None = None,
) -> dict:
    return {
        "role": "tool",
        "name": "read_file",
        "tool_call_id": f"call-{index}",
        "content": content if content is not None else f"result-{index}",
        "_compressible": compressible,
        "_message_id": f"tool-{index}",
        "_created_at": "2026-08-29T00:00:01+00:00",
    }


def _tool_image(
    associated_message_id: str | None,
    *,
    name: str = SCREENSHOT_MESSAGE_NAME,
    image_count: int = 1,
) -> dict:
    message = {
        "role": "user",
        "name": name,
        "content": [
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
            for _ in range(image_count)
        ],
        "_message_id": f"image-{associated_message_id}-{name}",
        "_created_at": "2026-08-29T00:00:02+00:00",
    }
    if associated_message_id is not None:
        message["_associated_message_id"] = associated_message_id
    return message


def test_screen_tools_allow_old_results_to_be_compacted() -> None:
    assert screenshot.compressible is True
    assert control_screen.compressible is True
    assert control_screen_with_vlm.compressible is True


def test_first_layer_keeps_latest_five_compressible_tool_results() -> None:
    messages: list[dict] = []
    contents = [f"{index}-" + "中" * 50 for index in range(7)]
    for index in range(7):
        messages.extend([
            _tool_call(f"ai-{index}", f"call-{index}"),
            _tool_result(index, content=contents[index]),
        ])
    messages.extend([_tool_call("ai-write", "call-write"), _tool_result(8, compressible=False)])

    changed = ContextCompressionPolicy().compress_old_tool_results(messages)

    assert changed == 2
    assert messages[1]["content"] == COMPRESSED_TOOL_RESULT
    assert messages[3]["content"] == COMPRESSED_TOOL_RESULT
    assert [
        message["content"]
        for message in messages
        if message.get("role") == "tool" and message.get("_compressible") is True
    ][-5:] == contents[2:]
    assert messages[-1]["content"] == "result-8"


def test_first_layer_never_compresses_pending_tool_batch() -> None:
    messages: list[dict] = []
    for index in range(7):
        messages.extend([
            _tool_call(f"ai-{index}", f"call-{index}"),
            _tool_result(index, content="中" * 50),
            _tool_image(f"tool-{index}"),
        ])

    changed = ContextCompressionPolicy().compress_old_tool_results(
        messages,
        keep_latest=0,
        protected_tool_call_ids={"call-0", "call-1"},
    )

    assert changed == 10
    protected_tools = [
        message for message in messages
        if message.get("tool_call_id") in {"call-0", "call-1"}
    ]
    assert [message["content"] for message in protected_tools] == ["中" * 50, "中" * 50]
    assert {
        message.get("_associated_message_id")
        for message in messages
        if message.get("name") == SCREENSHOT_MESSAGE_NAME
    } == {"tool-0", "tool-1"}


@pytest.mark.parametrize(
    ("content", "should_compress"),
    [
        ("中" * 49, False),
        ("中" * 50, True),
        ("x" * 196, False),
        ("x" * 200, True),
    ],
)
def test_first_layer_uses_weighted_fifty_character_text_threshold(
    content: str,
    should_compress: bool,
) -> None:
    messages = [_tool_result(0, content=content)]

    changed = ContextCompressionPolicy().compress_old_tool_results(messages, keep_latest=0)

    assert changed == int(should_compress)
    assert messages[0]["content"] == (
        COMPRESSED_TOOL_RESULT if should_compress else content
    )


def test_first_layer_removes_old_tool_image_but_keeps_short_text() -> None:
    messages: list[dict] = []
    for index in range(6):
        messages.extend([
            _tool_result(index, content="截屏成功"),
            _tool_image(f"tool-{index}"),
        ])

    changed = ContextCompressionPolicy().compress_old_tool_results(messages)

    assert changed == 1
    assert next(message for message in messages if message.get("_message_id") == "tool-0")["content"] == "截屏成功"
    assert {
        message.get("_associated_message_id")
        for message in messages
        if message.get("name") == SCREENSHOT_MESSAGE_NAME
    } == {f"tool-{index}" for index in range(1, 6)}


def test_first_layer_removes_only_strongly_associated_tool_images() -> None:
    target = _tool_result(0, content="中" * 50)
    associated_screenshot = _tool_image("tool-0")
    associated_compressed_screenshot = _tool_image(
        "tool-0",
        name=SCREENSHOT_COMPRESSED_NAME,
    )
    associated_mcp_image = _tool_image("tool-0", name=MCP_TOOL_IMAGE_MESSAGE_NAME)
    real_user_image = _tool_image("tool-0")
    real_user_image.pop("name")
    context_summary = _tool_image("tool-0", name=CONTEXT_SUMMARY_MESSAGE_NAME)
    unassociated_image = _tool_image(None)
    wrong_association = _tool_image("tool-other")
    noncompressible = _tool_result(1, compressible=False, content="中" * 50)
    noncompressible_image = _tool_image("tool-1")
    messages = [
        target,
        associated_screenshot,
        associated_compressed_screenshot,
        associated_mcp_image,
        real_user_image,
        context_summary,
        unassociated_image,
        wrong_association,
        noncompressible,
        noncompressible_image,
    ]
    messages_identity = id(messages)

    changed = ContextCompressionPolicy().compress_old_tool_results(messages, keep_latest=0)

    assert changed == 4
    assert id(messages) == messages_identity
    assert target["content"] == COMPRESSED_TOOL_RESULT
    assert associated_screenshot not in messages
    assert associated_compressed_screenshot not in messages
    assert associated_mcp_image not in messages
    assert real_user_image in messages
    assert context_summary in messages
    assert unassociated_image in messages
    assert wrong_association in messages
    assert noncompressible_image in messages


def test_first_layer_cleans_images_from_already_compressed_result_idempotently() -> None:
    messages = [
        _tool_result(0, content=COMPRESSED_TOOL_RESULT),
        _tool_image("tool-0"),
    ]

    first_changed = ContextCompressionPolicy().compress_old_tool_results(
        messages,
        keep_latest=0,
    )
    second_changed = ContextCompressionPolicy().compress_old_tool_results(
        messages,
        keep_latest=0,
    )

    assert first_changed == 1
    assert second_changed == 0
    assert messages == [_tool_result(0, content=COMPRESSED_TOOL_RESULT)]


def test_image_only_compaction_marks_prepared_state_as_changed() -> None:
    async def scenario():
        messages: list[dict] = []
        for index in range(6):
            messages.extend([
                _tool_call(f"ai-{index}", f"call-{index}"),
                _tool_result(index, content="截屏成功"),
            ])
            if index == 0:
                messages.append(_tool_image("tool-0", image_count=6))
        messages.append({
            "role": "user",
            "content": "继续",
            "_message_id": "human-latest",
            "_created_at": "2026-08-29T00:00:03+00:00",
        })
        state = AgentState(session_id="session-image-only", messages=messages)
        config = ContextStrategyConfig(compression_trigger_tokens_k=10)
        coordinator = ModelInputCoordinator(config, SimpleNamespace())

        prepared = await coordinator.prepare(
            state=state,
            system_prompt="fixed",
            tools=[],
            turn_context=RuntimeContext(
                latest_human_message_id="human-latest",
                captured_at=datetime(2026, 8, 29, tzinfo=timezone.utc),
            ),
        )
        return state, prepared

    state, prepared = asyncio.run(scenario())

    assert prepared.state_changed is True
    assert prepared.estimated_tokens < 10_000
    assert not any(
        message.get("_associated_message_id") == "tool-0"
        for message in state.messages
    )


def test_diary_coverage_port_removes_only_covered_context_summaries() -> None:
    class Coverage:
        async def has_diary_for_effective_date(self, effective_date: str) -> bool:
            return effective_date == "2026-08-28"

    async def scenario() -> tuple[int, list[str | None]]:
        budget = ContextBudgetService(ContextStrategyConfig(compression_trigger_tokens_k=10))
        coordinator = ContextCompactionCoordinator(
            budget,
            SimpleNamespace(),
            Coverage(),
        )
        state = AgentState.create_new("session")
        state.messages = [
            {
                "role": "user",
                "name": "context_summary",
                "content": "旧摘要",
                "_covered_effective_date": "2026-08-28",
            },
            {
                "role": "user",
                "name": "context_summary",
                "content": "未覆盖摘要",
                "_covered_effective_date": "2026-08-29",
            },
            {"role": "user", "content": "当前消息"},
        ]
        removed = await coordinator.drop_diary_covered_summaries(state)
        return removed, [message.get("content") for message in state.messages]

    assert asyncio.run(scenario()) == (1, ["未覆盖摘要", "当前消息"])


def test_context_budget_uses_actual_prompt_as_baseline_for_net_rough_delta() -> None:
    service = ContextBudgetService(ContextStrategyConfig(compression_trigger_tokens_k=10))
    first = service.assess(
        system_prompt="system",
        tools=[{"type": "function", "function": {"name": "x", "parameters": {}}}],
        messages=[{"role": "user", "content": "hello"}],
        baseline=None,
        accounting_key="openai-chat:model-a",
    )
    baseline = service.observe(first, actual_prompt_tokens=1_000)
    assert baseline is not None

    second = service.assess(
        system_prompt="system",
        tools=[{"type": "function", "function": {"name": "x", "parameters": {}}}],
        messages=[
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "world"},
            {"role": "user", "content": "next"},
        ],
        baseline=baseline,
        accounting_key="openai-chat:model-a",
    )

    assert second.source == "actual_delta"
    assert second.estimated_prompt_tokens == (
        1_000 + second.rough_prompt_tokens - first.rough_prompt_tokens
    )
    assert service.should_compact(10_000)
    assert not service.should_compact(9_999)


def test_context_budget_net_delta_accounts_for_replacement_and_deletion() -> None:
    service = ContextBudgetService(ContextStrategyConfig(compression_trigger_tokens_k=10))
    first = service.assess(
        system_prompt="system",
        tools=[],
        messages=[
            {"role": "user", "content": "old transient memory " + "x" * 600},
            {"role": "tool", "content": "large result " + "y" * 600},
        ],
        baseline=None,
        accounting_key="openai-chat:model-a",
    )
    baseline = service.observe(first, actual_prompt_tokens=2_000)
    assert baseline is not None

    second = service.assess(
        system_prompt="system",
        tools=[],
        messages=[
            {"role": "user", "content": "new transient memory"},
            {"role": "tool", "content": COMPRESSED_TOOL_RESULT},
            {"role": "user", "content": "tail"},
        ],
        baseline=baseline,
        accounting_key="openai-chat:model-a",
    )

    assert second.estimated_prompt_tokens == max(
        1,
        2_000 + second.rough_prompt_tokens - first.rough_prompt_tokens,
    )
    assert second.estimated_prompt_tokens < 2_000


@pytest.mark.parametrize(
    ("accounting_key", "system_prompt"),
    [
        ("openai-chat:model-b", "system"),
        ("openai-chat:model-a", "changed system"),
    ],
)
def test_context_budget_invalidates_incompatible_baseline(
    accounting_key: str,
    system_prompt: str,
) -> None:
    service = ContextBudgetService(ContextStrategyConfig(compression_trigger_tokens_k=10))
    first = service.assess(
        system_prompt="system",
        tools=[],
        messages=[{"role": "user", "content": "hello"}],
        baseline=None,
        accounting_key="openai-chat:model-a",
    )
    baseline = service.observe(first, actual_prompt_tokens=9_999)
    assert baseline is not None

    current = service.assess(
        system_prompt=system_prompt,
        tools=[],
        messages=[{"role": "user", "content": "hello"}],
        baseline=baseline,
        accounting_key=accounting_key,
    )

    assert current.source == "rough"
    assert current.estimated_prompt_tokens == current.rough_prompt_tokens


def test_context_budget_invalidates_baseline_when_tools_change() -> None:
    service = ContextBudgetService(ContextStrategyConfig(compression_trigger_tokens_k=10))
    first = service.assess(
        system_prompt="system",
        tools=[],
        messages=[{"role": "user", "content": "hello"}],
        baseline=None,
        accounting_key="openai-chat:model-a",
    )
    baseline = service.observe(first, actual_prompt_tokens=9_999)
    assert baseline is not None

    current = service.assess(
        system_prompt="system",
        tools=[{"type": "function", "function": {"name": "new_tool"}}],
        messages=[{"role": "user", "content": "hello"}],
        baseline=baseline,
        accounting_key="openai-chat:model-a",
    )

    assert current.source == "rough"
    assert current.estimated_prompt_tokens == current.rough_prompt_tokens


def test_context_budget_ignores_missing_actual_usage() -> None:
    service = ContextBudgetService(ContextStrategyConfig(compression_trigger_tokens_k=10))
    assessment = service.assess(
        system_prompt="system",
        tools=[],
        messages=[{"role": "user", "content": "hello"}],
        baseline=None,
        accounting_key="openai-chat:model-a",
    )

    assert service.observe(assessment, actual_prompt_tokens=0) is None


def test_token_baseline_survives_checkpoint_round_trip() -> None:
    state = AgentState.create_new("session")
    state.token_baseline = TokenBaseline(
        accounting_key="openai-chat:model-a",
        estimator_version=1,
        static_fingerprint="abc",
        rough_prompt_tokens=900,
        actual_prompt_tokens=1_000,
    )

    restored = AgentState.from_checkpoint(state.to_checkpoint())

    assert restored.token_baseline == state.token_baseline


def test_compression_trigger_is_configured_directly_in_k_tokens() -> None:
    defaults = ContextStrategyConfig()
    minimum = ContextStrategyConfig(compression_trigger_tokens_k=10)

    assert defaults.compression_trigger_tokens == 100_000
    assert minimum.compression_trigger_tokens == 10_000
    with pytest.raises(ValidationError):
        ContextStrategyConfig(compression_trigger_tokens_k=9)


def test_protocol_blocks_never_split_tool_call_from_results() -> None:
    messages = [
        {"role": "user", "content": "one"},
        _tool_call("ai-1", "call-1"),
        _tool_result(1),
        {"role": "assistant", "content": "done"},
        {"role": "user", "content": "latest"},
    ]

    blocks = ContextCompressionPolicy.protocol_blocks(messages)

    assert any(block == [1, 2] for block in blocks)


def test_transient_context_is_appended_to_copy_of_latest_human_message() -> None:
    original = [{
        "role": "user",
        "content": [{"type": "text", "text": "问题"}],
        "_message_id": "human-1",
        "_created_at": "2026-08-29T00:00:00+00:00",
    }]
    turn = RuntimeContext(
        latest_human_message_id="human-1",
        captured_at=datetime(2026, 8, 29, tzinfo=timezone.utc),
        recalled_memory="用户喜欢简洁回答",
    )

    projected = append_transient_context(original, turn)

    assert original[0]["content"] == [{"type": "text", "text": "问题"}]
    assert projected[0]["content"][-1]["type"] == "text"
    assert "[系统提示]" in projected[0]["content"][-1]["text"]
    assert "用户喜欢简洁回答" in projected[0]["content"][-1]["text"]


class _SummaryClient:
    def __init__(self, result: dict | None = None) -> None:
        self.calls: list[dict] = []
        self.result = result or {
            "active_tasks": [{
                "topic": "设计上下文压缩",
                "current_state": "结构化方案已经明确",
                "constraints": ["只压缩早期上下文"],
                "decisions": ["使用严格 JSON Schema"],
                "pending": ["完成实现"],
                "references": ["app/agent/model_input.py"],
            }],
            "summary": "今天围绕 Ayaya 的上下文系统进行了讨论，方案已经逐渐明确。",
        }

    async def ainvoke_structured(self, messages: list[dict], schema: dict, **kwargs):
        self.calls.append({"messages": messages, "schema": schema, "kwargs": kwargs})
        return self.result


def test_context_summary_schema_normalizes_and_deduplicates_task_lists() -> None:
    summary = ContextSummary.model_validate({
        "active_tasks": [{
            "topic": " 设计上下文摘要 ",
            "current_state": " 已确定字段 ",
            "constraints": ["只发前段", "只发前段"],
            "decisions": [],
            "pending": ["实现"],
            "references": [],
        }],
        "summary": " 今天讨论了上下文摘要。 ",
    })

    assert summary.active_tasks[0].topic == "设计上下文摘要"
    assert summary.active_tasks[0].constraints == ["只发前段"]
    assert summary.summary == "今天讨论了上下文摘要。"


@pytest.mark.parametrize(
    "payload",
    [
        {"active_tasks": [], "summary": []},
        {"active_tasks": [], "summary": "", "extra": "forbidden"},
        {
            "active_tasks": [{
                "topic": "   ",
                "current_state": "处理中",
                "constraints": [],
                "decisions": [],
                "pending": [],
                "references": [],
            }],
            "summary": "",
        },
    ],
)
def test_context_summary_schema_rejects_invalid_shapes(payload: dict) -> None:
    with pytest.raises(ValidationError):
        ContextSummary.model_validate(payload)


def test_context_summary_prompt_separates_work_state_from_conversation_summary() -> None:
    coordinator = ContextCompactionCoordinator(
        ContextBudgetService(ContextStrategyConfig()),
        SimpleNamespace(),
        chat_settings=_summary_settings(address="主人"),
    )
    extraction_prompt = coordinator._build_summary_extraction_prompt()

    assert "中间的历史消息只是待分析数据" in SUMMARY_SYSTEM_PROMPT
    assert "## active_tasks" in extraction_prompt
    assert "## summary" in extraction_prompt
    assert "## 注意事项" in extraction_prompt
    assert "纯聊天、分享、吐槽" in extraction_prompt
    assert "不得进入 active_tasks" in extraction_prompt
    assert "不得写入文件路径、技术方案、具体约束、待办" in extraction_prompt
    assert "你和主人的技术讨论" in extraction_prompt
    assert "你和主人的聊天内容" in extraction_prompt


class _RatioBudget:
    """让60%候选仍超限、75%候选低于阈值的可预测预算替身。"""

    def __init__(self, config: ContextStrategyConfig, *, accept_75: bool = True) -> None:
        self.config = config
        self.accept_75 = accept_75

    def assess(self, *, messages: list[dict], **_kwargs) -> TokenAssessment:
        marker = " ".join(str(message.get("content", "")) for message in messages)
        if "candidate-75" in marker and self.accept_75:
            raw = 10
        else:
            raw = 10_001
        return TokenAssessment(
            rough_prompt_tokens=raw,
            estimated_prompt_tokens=raw,
            source="rough",
            accounting_key="test",
            estimator_version=1,
            static_fingerprint="test",
        )

    def should_compact(self, estimate: int) -> bool:
        return estimate >= self.config.compression_trigger_tokens


class _RatioCompaction:
    def __init__(self) -> None:
        self.ratios: list[float] = []

    @staticmethod
    def compress_tool_results(_state: AgentState) -> int:
        return 0

    @staticmethod
    async def drop_diary_covered_summaries(_state: AgentState) -> int:
        return 0

    async def summarize_candidate(self, frozen: list[dict], ratio: float) -> list[dict]:
        self.ratios.append(ratio)
        return [
            {
                "role": "user",
                "name": "context_summary",
                "content": f"candidate-{int(ratio * 100)}",
                "_message_id": f"summary-{ratio}",
                "_created_at": "2026-08-29T00:00:00+00:00",
            },
            dict(frozen[-1]),
        ]


def test_model_input_coordinator_replaces_old_turns_with_state_only_summary() -> None:
    async def scenario():
        state = AgentState(session_id="session-summary")
        created_at = "2026-08-29T00:00:00+00:00"
        for index in range(12):
            state.messages.extend([
                {
                    "role": "user",
                    "content": f"第{index}轮问题：" + "中" * 600,
                    "_message_id": f"human-{index}",
                    "_created_at": created_at,
                },
                {
                    "role": "assistant",
                    "content": f"第{index}轮回答：" + "答" * 600,
                    "_message_id": f"assistant-{index}",
                    "_created_at": created_at,
                },
            ])
        summary_client = _SummaryClient()
        config = ContextStrategyConfig(compression_trigger_tokens_k=10)
        coordinator = ModelInputCoordinator(
            config,
            summary_client,
            chat_settings=SimpleNamespace(
                name="Ayaya",
                feature="温柔",
                character="助手",
                address="用户",
                characteristic="忠实记录共同经历。",
            ),
        )
        turn = RuntimeContext(
            latest_human_message_id="human-11",
            captured_at=datetime(2026, 8, 29, tzinfo=timezone.utc),
            recalled_memory="仅存在于本轮输入的召回记忆",
        )

        prepared = await coordinator.prepare(
            state=state,
            system_prompt="固定系统提示",
            tools=[],
            turn_context=turn,
        )
        return state, summary_client, config, prepared

    state, summary_client, config, prepared = asyncio.run(scenario())

    summaries = [message for message in state.messages if message.get("name") == "context_summary"]
    assert prepared.state_changed is True
    assert len(summary_client.calls) == 1
    summary_call = summary_client.calls[0]
    assert summary_call["schema"] == CONTEXT_SUMMARY_SCHEMA
    assert summary_call["kwargs"] == {"temperature": 0.2}
    assert summary_call["messages"][0]["role"] == "system"
    assert "你是Ayaya" in summary_call["messages"][0]["content"]
    assert "固定系统提示" not in summary_call["messages"][0]["content"]
    extraction_prompt = summary_call["messages"][-1]["content"]
    assert summary_call["messages"][-1]["role"] == "user"
    assert isinstance(extraction_prompt, str)
    assert "## active_tasks" in extraction_prompt
    assert "## summary" in extraction_prompt
    assert "第11轮问题" not in str(summary_call["messages"])
    assert "仅存在于本轮输入的召回记忆" not in str(summary_call["messages"])
    assert len(summaries) == 1
    assert summaries[0]["content"].startswith("[系统提示：前情摘要]")
    _, serialized_summary = summaries[0]["content"].split("\n", 1)
    assert json.loads(serialized_summary) == summary_client.result
    assert summaries[0]["_summary_schema_version"] == 1
    assert summaries[0]["_covered_through_message_id"].startswith("assistant-")
    assert summaries[0]["_covered_effective_date"] == "2026-08-29"
    assert "_covered_through_at" not in summaries[0]
    assert any(message.get("_message_id") == "human-11" for message in state.messages)
    assert all("仅存在于本轮输入的召回记忆" not in str(message.get("content")) for message in state.messages)
    assert all(extraction_prompt not in str(message.get("content")) for message in state.messages)
    assert any("仅存在于本轮输入的召回记忆" in str(message.get("content")) for message in prepared.messages)
    assert prepared.estimated_tokens < config.compression_trigger_tokens


def test_context_summary_persona_is_built_directly_from_optional_chat_settings() -> None:
    async def scenario() -> list[dict]:
        client = _SummaryClient()
        coordinator = ContextCompactionCoordinator(
            ContextBudgetService(ContextStrategyConfig()),
            client,
            chat_settings=SimpleNamespace(
                name=None,
                feature=None,
                character=None,
                address=None,
                characteristic=None,
            ),
        )
        frozen = [
            {
                "role": "user",
                "content": "旧消息",
                "_message_id": "human-old",
                "_created_at": "2026-08-28T00:00:00+00:00",
            },
            {
                "role": "user",
                "content": "最新消息",
                "_message_id": "human-latest",
                "_created_at": "2026-08-29T00:00:00+00:00",
            },
        ]
        await coordinator.summarize_candidate(frozen, 1.0)
        return client.calls[0]["messages"]

    messages = asyncio.run(scenario())
    system_prompt = messages[0]["content"]

    assert "你是AI助手" in system_prompt
    assert "称呼用户为用户" in system_prompt
    assert "None" not in system_prompt


def test_repeated_compaction_advances_coverage_from_old_summary_to_latest_raw_message() -> None:
    async def scenario() -> tuple[list[dict], _SummaryClient]:
        old_payload = {"active_tasks": [], "summary": "旧摘要"}
        frozen = [
            {
                "role": "user",
                "name": "context_summary",
                "content": "[系统提示：前情摘要]\n" + json.dumps(old_payload, ensure_ascii=False),
                "_message_id": "summary-old",
                "_summary_schema_version": 1,
                "_covered_through_message_id": "assistant-original",
                "_covered_effective_date": "2026-08-28",
                "_created_at": "2026-08-29T00:00:00+00:00",
            },
            {
                "role": "user",
                "content": "继续处理",
                "_message_id": "human-work",
                "_created_at": "2026-08-29T05:00:00+00:00",
            },
            {
                "role": "assistant",
                "content": "取得新进展",
                "_message_id": "assistant-work",
                "_created_at": "2026-08-29T05:10:00+00:00",
            },
            {
                "role": "user",
                "content": "最新消息",
                "_message_id": "human-latest",
                "_created_at": "2026-08-29T06:00:00+00:00",
            },
        ]
        client = _SummaryClient()
        coordinator = ContextCompactionCoordinator(
            ContextBudgetService(ContextStrategyConfig()),
            client,
            chat_settings=_summary_settings(),
        )
        return await coordinator.summarize_candidate(frozen, 1.0), client

    candidate, client = asyncio.run(scenario())
    summary = next(message for message in candidate if message.get("name") == "context_summary")

    assert summary["_covered_through_message_id"] == "assistant-work"
    assert summary["_covered_effective_date"] == "2026-08-29"
    assert "最新消息" not in str(client.calls[0]["messages"])
    assert len([message for message in candidate if message.get("name") == "context_summary"]) == 1


def test_legacy_summary_uses_its_own_message_id_as_coverage_fallback() -> None:
    async def scenario() -> list[dict]:
        frozen = [
            {
                "role": "user",
                "name": "context_summary",
                "content": "旧的自由文本摘要",
                "_message_id": "legacy-summary",
                "_covered_effective_date": "2026-08-28",
                "_covered_through_at": "2026-08-28T18:00:00+00:00",
                "_created_at": "2026-08-29T00:00:00+00:00",
            },
            {
                "role": "user",
                "content": "最新消息",
                "_message_id": "human-latest",
                "_created_at": "2026-08-29T06:00:00+00:00",
            },
        ]
        coordinator = ContextCompactionCoordinator(
            ContextBudgetService(ContextStrategyConfig()),
            _SummaryClient(),
            chat_settings=_summary_settings(),
        )
        return await coordinator.summarize_candidate(frozen, 1.0)

    candidate = asyncio.run(scenario())
    summary = next(message for message in candidate if message.get("name") == "context_summary")

    assert summary["_covered_through_message_id"] == "legacy-summary"
    assert summary["_covered_effective_date"] == "2026-08-28"
    assert "_covered_through_at" not in summary


def test_context_summary_accepts_valid_structure_regardless_of_estimated_size() -> None:
    oversized_task = {
        "topic": "任务",
        "current_state": "中" * 800,
        "constraints": ["约" * 400] * 8,
        "decisions": ["定" * 400] * 8,
        "pending": ["待" * 400] * 8,
        "references": ["引" * 400] * 8,
    }
    client = _SummaryClient({
        "active_tasks": [oversized_task] * 8,
        "summary": "概" * 2_000,
    })
    frozen = [
        {
            "role": "user",
            "content": "旧消息",
            "_message_id": "human-old",
            "_created_at": "2026-08-28T00:00:00+00:00",
        },
        {
            "role": "user",
            "content": "最新消息",
            "_message_id": "human-latest",
            "_created_at": "2026-08-29T00:00:00+00:00",
        },
    ]
    coordinator = ContextCompactionCoordinator(
        ContextBudgetService(ContextStrategyConfig()),
        client,
        chat_settings=_summary_settings(),
    )

    candidate = asyncio.run(coordinator.summarize_candidate(frozen, 1.0))

    assert len(candidate) == 2
    assert candidate[0]["name"] == CONTEXT_SUMMARY_MESSAGE_NAME
    assert "中" * 800 in candidate[0]["content"]


def test_invalid_structured_summary_does_not_replace_state_messages() -> None:
    async def scenario() -> tuple[AgentState, dict]:
        state = AgentState(session_id="session-invalid-summary")
        for index in range(12):
            state.messages.extend([
                {
                    "role": "user",
                    "content": f"第{index}轮问题：" + "中" * 600,
                    "_message_id": f"human-{index}",
                    "_created_at": "2026-08-29T00:00:00+00:00",
                },
                {
                    "role": "assistant",
                    "content": f"第{index}轮回答：" + "答" * 600,
                    "_message_id": f"assistant-{index}",
                    "_created_at": "2026-08-29T00:01:00+00:00",
                },
            ])
        original = state.to_checkpoint()
        client = _SummaryClient({
            "active_tasks": [],
            "summary": "格式之外还有字段",
            "extra": "forbidden",
        })
        coordinator = ModelInputCoordinator(
            ContextStrategyConfig(compression_trigger_tokens_k=10),
            client,
            chat_settings=_summary_settings(),
        )
        with pytest.raises(ValidationError):
            await coordinator.prepare(
                state=state,
                system_prompt="fixed",
                tools=[],
                turn_context=RuntimeContext(
                    latest_human_message_id="human-11",
                    captured_at=datetime(2026, 8, 29, tzinfo=timezone.utc),
                ),
            )
        return state, original

    state, original = asyncio.run(scenario())

    assert state.to_checkpoint() == original


def test_model_input_expands_from_frozen_source_to_75_percent() -> None:
    async def scenario(*, accept_75: bool):
        state = AgentState(session_id="session-expand", messages=[
            {
                "role": "user",
                "content": "old",
                "_message_id": "human-old",
                "_created_at": "2026-08-28T00:00:00+00:00",
            },
            {
                "role": "user",
                "content": "latest",
                "_message_id": "human-latest",
                "_created_at": "2026-08-29T00:00:00+00:00",
            },
        ])
        config = ContextStrategyConfig(compression_trigger_tokens_k=10)
        coordinator = ModelInputCoordinator(config, _SummaryClient())
        coordinator.budget = _RatioBudget(config, accept_75=accept_75)
        compaction = _RatioCompaction()
        coordinator.compaction = compaction
        turn = RuntimeContext(
            latest_human_message_id="human-latest",
            captured_at=datetime(2026, 8, 29, tzinfo=timezone.utc),
        )
        prepared = await coordinator.prepare(
            state=state,
            system_prompt="fixed",
            tools=[],
            turn_context=turn,
        )
        return state, prepared, compaction

    state, prepared, compaction = asyncio.run(scenario(accept_75=True))

    assert compaction.ratios == [0.60, 0.75]
    assert state.messages[0]["content"] == "candidate-75"
    assert prepared.estimated_tokens == 10


def test_model_input_stops_before_main_model_when_75_percent_is_still_over_budget() -> None:
    async def scenario():
        state = AgentState(session_id="session-overflow", messages=[
            {
                "role": "user",
                "content": "old",
                "_message_id": "human-old",
                "_created_at": "2026-08-28T00:00:00+00:00",
            },
            {
                "role": "user",
                "content": "latest",
                "_message_id": "human-latest",
                "_created_at": "2026-08-29T00:00:00+00:00",
            },
        ])
        original = state.to_checkpoint()
        config = ContextStrategyConfig(compression_trigger_tokens_k=10)
        coordinator = ModelInputCoordinator(config, _SummaryClient())
        coordinator.budget = _RatioBudget(config, accept_75=False)
        compaction = _RatioCompaction()
        coordinator.compaction = compaction
        with pytest.raises(ContextOverflowError):
            await coordinator.prepare(
                state=state,
                system_prompt="fixed",
                tools=[],
                turn_context=RuntimeContext(
                    latest_human_message_id="human-latest",
                    captured_at=datetime(2026, 8, 29, tzinfo=timezone.utc),
                ),
            )
        return state, original, compaction

    state, original, compaction = asyncio.run(scenario())

    assert compaction.ratios == [0.60, 0.75]
    assert state.to_checkpoint() == original
