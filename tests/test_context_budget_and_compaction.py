import asyncio
import json
import pytest
from dataclasses import replace
from pydantic import ValidationError
from app.agent.context import ContextStrategyConfig, RuntimeContext
from app.agent.context._budget import (
    ContextBudgetService,
    TokenAssessment,
)
from app.agent.context._compaction import (
    COMPRESSED_TOOL_RESULT,
    COMPRESSED_SCREENSHOT_PLACEHOLDER,
    ContextCompressionPolicy,
)
from app.agent.context._projection import append_transient_context
from app.agent.state.messages import (
    AssistantMessage,
    AvailableMedia,
    CompactedMedia,
    CONTEXT_SUMMARY_MESSAGE_NAME,
    ContextSummaryMessage,
    HumanMessage,
    MCP_TOOL_IMAGE_MESSAGE_NAME,
    SystemMessage,
    SCREENSHOT_COMPRESSED_NAME,
    SCREENSHOT_MESSAGE_NAME,
    ToolCall,
    ToolMediaMessage,
    ToolMessage,
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


def _tool_call(message_id: str, tool_call_id: str) -> AssistantMessage:
    return AssistantMessage(
        message_id=message_id,
        created_at=datetime(2026, 8, 29, tzinfo=timezone.utc),
        text="",
        tool_calls=(ToolCall(id=tool_call_id, name="read_file", args={}),),
    )


def _tool_result(
    index: int,
    *,
    compressible: bool = True,
    content: str | None = None,
) -> ToolMessage:
    return ToolMessage(
        message_id=f"tool-{index}",
        created_at=datetime(2026, 8, 29, 0, 0, 1, tzinfo=timezone.utc),
        tool_name="read_file",
        tool_call_id=f"call-{index}",
        content=content if content is not None else f"result-{index}",
        compressible=compressible,
    )


def _tool_image(
    associated_message_id: str | None,
    *,
    name: str = SCREENSHOT_MESSAGE_NAME,
    image_count: int = 1,
) -> ToolMediaMessage:
    media_kind = {
        SCREENSHOT_MESSAGE_NAME: "screenshot",
        MCP_TOOL_IMAGE_MESSAGE_NAME: "mcp",
    }.get(name, "tool")
    return ToolMediaMessage(
        message_id=f"image-{associated_message_id}-{name}",
        created_at=datetime(2026, 8, 29, 0, 0, 2, tzinfo=timezone.utc),
        tool_name="screenshot" if media_kind == "screenshot" else "read_file",
        media_kind=media_kind,
        text="[系统消息]工具返回图片",
        payload=AvailableMedia(
            urls=tuple("data:image/png;base64,AAAA" for _ in range(image_count)),
        ),
        associated_message_id=associated_message_id,
    )


def _human(index: int) -> HumanMessage:
    return HumanMessage(
        message_id=f"human-{index}",
        created_at=datetime(2026, 8, 29, 0, 0, index, tzinfo=timezone.utc),
        text=f"human-{index}",
    )


def _state(session_id: str, messages) -> AgentState:
    state = AgentState.create_new(session_id)
    checkpoint = state.to_checkpoint()
    checkpoint["messages"] = [message.to_storage_format() for message in messages]
    return AgentState.from_checkpoint(checkpoint)


def test_screenshot_policy_expires_after_two_real_human_messages() -> None:
    screenshot_message = replace(
        _tool_image("tool-0"),
        payload=AvailableMedia(
            urls=("data:image/png;base64,AAAA",),
            storage_refs=("screenshot/shot.png",),
        ),
    )
    messages = [
        screenshot_message,
        SystemMessage(text="系统提示"),
        ToolMessage(content="工具结果", tool_name="read_file", tool_call_id="call-0"),
        _tool_image("tool-mcp", name=MCP_TOOL_IMAGE_MESSAGE_NAME),
        _human(1),
        _tool_image("tool-1"),
        _human(2),
    ]

    candidate, changed = ContextCompressionPolicy().compress_screenshots(
        messages,
        ttl_human_messages=2,
        max_screenshots=10,
    )

    assert changed == 1
    assert isinstance(candidate[0].payload, CompactedMedia)
    assert candidate[0].payload.storage_refs == ("screenshot/shot.png",)
    assert isinstance(candidate[5].payload, AvailableMedia)


def test_screenshot_policy_ignores_system_tool_and_injected_user_messages() -> None:
    messages = [
        _tool_image("tool-0"),
        SystemMessage(text="系统提示"),
        ToolMessage(content="工具结果", tool_name="read_file", tool_call_id="call-0"),
        _tool_image("tool-mcp", name=MCP_TOOL_IMAGE_MESSAGE_NAME),
        _tool_image("tool-1"),
        _human(1),
    ]

    candidate, changed = ContextCompressionPolicy().compress_screenshots(
        messages,
        ttl_human_messages=2,
        max_screenshots=10,
    )

    assert changed == 0
    assert candidate == tuple(messages)
    assert all(
        isinstance(message.payload, AvailableMedia)
        for message in candidate
        if isinstance(message, ToolMediaMessage)
    )


def test_screenshot_policy_keeps_only_two_latest_in_long_task() -> None:
    messages = [_tool_image(f"tool-{index}") for index in range(3)]

    candidate, changed = ContextCompressionPolicy().compress_screenshots(
        messages,
        ttl_human_messages=2,
        max_screenshots=2,
    )

    assert changed == 1
    assert [type(message.payload) for message in candidate] == [
        CompactedMedia,
        AvailableMedia,
        AvailableMedia,
    ]


@pytest.mark.parametrize(
    ("ttl_human_messages", "max_screenshots"),
    [(0, 2), (2, 0)],
)
def test_screenshot_policy_zero_compresses_immediately_and_is_idempotent(
    ttl_human_messages: int,
    max_screenshots: int,
) -> None:
    messages = [_tool_image("tool-0")]

    first, first_changed = ContextCompressionPolicy().compress_screenshots(
        messages,
        ttl_human_messages=ttl_human_messages,
        max_screenshots=max_screenshots,
    )
    second, second_changed = ContextCompressionPolicy().compress_screenshots(
        first,
        ttl_human_messages=ttl_human_messages,
        max_screenshots=max_screenshots,
    )

    assert first_changed == 1
    assert second_changed == 0
    assert isinstance(first[0].payload, CompactedMedia)
    assert second == first


def test_screenshot_policy_removes_large_payload_from_production_shape() -> None:
    image_payload = "data:image/png;base64," + "A" * 100_000
    messages = []
    for index in range(16):
        messages.append(replace(
            _tool_image(f"tool-{index}"),
            payload=AvailableMedia(urls=(image_payload,)),
        ))
    before_size = len(json.dumps([message.to_storage_format() for message in messages], ensure_ascii=False))

    candidate, changed = ContextCompressionPolicy().compress_screenshots(
        messages,
        ttl_human_messages=2,
        max_screenshots=2,
    )
    after_json = json.dumps([message.to_storage_format() for message in candidate], ensure_ascii=False)

    assert changed == 14
    assert sum(
        isinstance(message.payload, AvailableMedia) for message in candidate
    ) == 2
    assert "data:image/png;base64" in after_json
    assert after_json.count("data:image/png;base64") == 2
    assert len(after_json) < before_size * 0.2


def test_screenshot_compaction_runs_below_token_threshold_and_marks_state_changed() -> None:
    async def scenario():
        screenshots = [_tool_image(f"tool-{index}") for index in range(3)]
        state = _state("session-screenshot-limit", [*screenshots, _human(1)])
        coordinator = ModelInputCoordinator(
            ContextStrategyConfig(
                compression_trigger_tokens_k=100,
                screenshot_ttl_human_messages=2,
                max_screenshots_in_context=2,
            ),
            SimpleNamespace(),
        )

        prepared = await coordinator.prepare(
            state=state,
            system_prompt="fixed",
            tools=[],
            turn_context=RuntimeContext(
                latest_human_message_id="human-1",
                captured_at=datetime(2026, 8, 29, tzinfo=timezone.utc),
            ),
        )
        return state, prepared

    state, prepared = asyncio.run(scenario())

    assert prepared.state_changed is True
    assert prepared.estimated_tokens < 100_000
    assert [type(message.payload) for message in state.messages[:3]] == [
        CompactedMedia, AvailableMedia, AvailableMedia,
    ]
    compressed = state.messages[0]
    assert isinstance(compressed, ToolMediaMessage)
    assert isinstance(compressed.payload, CompactedMedia)
    assert "base64" not in json.dumps(compressed.to_storage_format())
    assert prepared.messages[0]["role"] == "system"
    assert any(
        message.get("name") == SCREENSHOT_COMPRESSED_NAME
        for message in prepared.messages
    )


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

    candidate, changed = ContextCompressionPolicy().compress_old_tool_results(messages)

    assert changed == 2
    assert candidate[1].content == COMPRESSED_TOOL_RESULT
    assert candidate[3].content == COMPRESSED_TOOL_RESULT
    assert [
        message.content
        for message in candidate
        if isinstance(message, ToolMessage) and message.compressible
    ][-5:] == contents[2:]
    assert candidate[-1].content == "result-8"


def test_first_layer_never_compresses_pending_tool_batch() -> None:
    messages: list[dict] = []
    for index in range(7):
        messages.extend([
            _tool_call(f"ai-{index}", f"call-{index}"),
            _tool_result(index, content="中" * 50),
            _tool_image(f"tool-{index}"),
        ])

    candidate, changed = ContextCompressionPolicy().compress_old_tool_results(
        messages,
        keep_latest=0,
        protected_tool_call_ids={"call-0", "call-1"},
    )

    assert changed == 10
    protected_tools = [
        message for message in candidate
        if isinstance(message, ToolMessage) and message.tool_call_id in {"call-0", "call-1"}
    ]
    assert [message.content for message in protected_tools] == ["中" * 50, "中" * 50]
    assert {
        message.associated_message_id
        for message in candidate
        if isinstance(message, ToolMediaMessage)
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

    candidate, changed = ContextCompressionPolicy().compress_old_tool_results(messages, keep_latest=0)

    assert changed == int(should_compress)
    assert candidate[0].content == (
        COMPRESSED_TOOL_RESULT if should_compress else content
    )


def test_first_layer_removes_old_tool_image_but_keeps_short_text() -> None:
    messages: list[dict] = []
    for index in range(6):
        messages.extend([
            _tool_result(index, content="截屏成功"),
            _tool_image(f"tool-{index}"),
        ])

    candidate, changed = ContextCompressionPolicy().compress_old_tool_results(messages)

    assert changed == 1
    assert next(message for message in candidate if message.message_id == "tool-0").content == "截屏成功"
    assert {
        message.associated_message_id
        for message in candidate
        if isinstance(message, ToolMediaMessage)
    } == {f"tool-{index}" for index in range(1, 6)}


def test_first_layer_removes_only_strongly_associated_tool_images() -> None:
    target = _tool_result(0, content="中" * 50)
    associated_screenshot = _tool_image("tool-0")
    associated_compressed_screenshot = replace(
        _tool_image("tool-0"), payload=CompactedMedia(),
    )
    associated_mcp_image = _tool_image("tool-0", name=MCP_TOOL_IMAGE_MESSAGE_NAME)
    real_user_image = HumanMessage(text="user image", image_urls=("data:image/png;base64,AAAA",))
    context_summary = ContextSummaryMessage(
        content="summary", summary_schema_version=1,
        covered_through_message_id="tool-0", covered_effective_date="2026-08-29",
        compression_ratio=0.5,
    )
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
    candidate, changed = ContextCompressionPolicy().compress_old_tool_results(messages, keep_latest=0)

    assert changed == 4
    assert next(message for message in candidate if message.message_id == target.message_id).content == COMPRESSED_TOOL_RESULT
    assert associated_screenshot not in candidate
    assert associated_compressed_screenshot not in candidate
    assert associated_mcp_image not in candidate
    assert real_user_image in candidate
    assert context_summary in candidate
    assert unassociated_image in candidate
    assert wrong_association in candidate
    assert noncompressible_image in candidate


def test_first_layer_cleans_images_from_already_compressed_result_idempotently() -> None:
    messages = [
        _tool_result(0, content=COMPRESSED_TOOL_RESULT),
        _tool_image("tool-0"),
    ]

    first, first_changed = ContextCompressionPolicy().compress_old_tool_results(
        messages,
        keep_latest=0,
    )
    second, second_changed = ContextCompressionPolicy().compress_old_tool_results(
        first,
        keep_latest=0,
    )

    assert first_changed == 1
    assert second_changed == 0
    assert first == (_tool_result(0, content=COMPRESSED_TOOL_RESULT),)
    assert second == first


def test_image_only_compaction_marks_prepared_state_as_changed() -> None:
    async def scenario():
        messages = []
        for index in range(6):
            messages.extend([
                _tool_call(f"ai-{index}", f"call-{index}"),
                _tool_result(index, content="截屏成功"),
            ])
            if index == 0:
                messages.append(_tool_image("tool-0", image_count=6))
        messages.append(HumanMessage(
            message_id="human-latest",
            created_at=datetime(2026, 8, 29, 0, 0, 3, tzinfo=timezone.utc),
            text="继续",
        ))
        state = _state("session-image-only", messages)
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
        isinstance(message, ToolMediaMessage)
        and message.associated_message_id == "tool-0"
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
        state.commit_compaction((
            ContextSummaryMessage(
                content="旧摘要", summary_schema_version=1,
                covered_through_message_id="old", covered_effective_date="2026-08-28",
                compression_ratio=0.5,
            ),
            ContextSummaryMessage(
                content="未覆盖摘要", summary_schema_version=1,
                covered_through_message_id="new", covered_effective_date="2026-08-29",
                compression_ratio=0.5,
            ),
        ))
        state.add_user_message("当前消息")
        removed = await coordinator.drop_diary_covered_summaries(state)
        return removed, [message.text for message in state.messages]

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
        HumanMessage(text="one"),
        _tool_call("ai-1", "call-1"),
        _tool_result(1),
        AssistantMessage(text="done"),
        HumanMessage(text="latest"),
    ]

    blocks = ContextCompressionPolicy.protocol_blocks(messages)

    assert any(block == [1, 2] for block in blocks)


def test_transient_context_is_appended_to_copy_of_latest_human_message() -> None:
    original = (HumanMessage(
        text="问题",
        message_id="human-1",
        created_at=datetime(2026, 8, 29, tzinfo=timezone.utc),
    ),)
    turn = RuntimeContext(
        latest_human_message_id="human-1",
        captured_at=datetime(2026, 8, 29, tzinfo=timezone.utc),
        recalled_memory="用户喜欢简洁回答",
    )

    projected = append_transient_context(original, turn)

    assert original[0].text == "问题"
    assert isinstance(projected[0], HumanMessage)
    assert "[系统提示]" in projected[0].text
    assert "用户喜欢简洁回答" in projected[0].text


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

    async def summarize_candidate(self, frozen, ratio: float):
        self.ratios.append(ratio)
        return (
            ContextSummaryMessage(
                content=f"candidate-{int(ratio * 100)}",
                message_id=f"summary-{ratio}",
                created_at=datetime(2026, 8, 29, tzinfo=timezone.utc),
                summary_schema_version=1,
                covered_through_message_id=frozen[-2].message_id,
                covered_effective_date="2026-08-29",
                compression_ratio=ratio,
            ),
            frozen[-1],
        )


def test_model_input_coordinator_replaces_old_turns_with_state_only_summary() -> None:
    async def scenario():
        messages = []
        created_at = datetime(2026, 8, 29, tzinfo=timezone.utc)
        for index in range(12):
            messages.extend([
                HumanMessage(
                    text=f"第{index}轮问题：" + "中" * 600,
                    message_id=f"human-{index}", created_at=created_at,
                ),
                AssistantMessage(
                    text=f"第{index}轮回答：" + "答" * 600,
                    message_id=f"assistant-{index}", created_at=created_at,
                ),
            ])
        state = _state("session-summary", messages)
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

    summaries = [message for message in state.messages if isinstance(message, ContextSummaryMessage)]
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
    assert summaries[0].content.startswith("[系统提示：前情摘要]")
    _, serialized_summary = summaries[0].content.split("\n", 1)
    assert json.loads(serialized_summary) == summary_client.result
    assert summaries[0].summary_schema_version == 1
    assert summaries[0].covered_through_message_id.startswith("assistant-")
    assert summaries[0].covered_effective_date == "2026-08-29"
    assert any(message.message_id == "human-11" for message in state.messages)
    assert all("仅存在于本轮输入的召回记忆" not in message.text for message in state.messages)
    assert all(extraction_prompt not in message.text for message in state.messages)
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
        frozen = (
            HumanMessage(text="旧消息", message_id="human-old", created_at=datetime(2026, 8, 28, tzinfo=timezone.utc)),
            HumanMessage(text="最新消息", message_id="human-latest", created_at=datetime(2026, 8, 29, tzinfo=timezone.utc)),
        )
        await coordinator.summarize_candidate(frozen, 1.0)
        return client.calls[0]["messages"]

    messages = asyncio.run(scenario())
    system_prompt = messages[0]["content"]

    assert "你是AI助手" in system_prompt
    assert "称呼用户为用户" in system_prompt
    assert "None" not in system_prompt


def test_repeated_compaction_advances_coverage_from_old_summary_to_latest_raw_message() -> None:
    async def scenario():
        old_payload = {"active_tasks": [], "summary": "旧摘要"}
        frozen = (
            ContextSummaryMessage(
                content="[系统提示：前情摘要]\n" + json.dumps(old_payload, ensure_ascii=False),
                message_id="summary-old", summary_schema_version=1,
                covered_through_message_id="assistant-original",
                covered_effective_date="2026-08-28", compression_ratio=0.5,
                created_at=datetime(2026, 8, 29, tzinfo=timezone.utc),
            ),
            HumanMessage(text="继续处理", message_id="human-work", created_at=datetime(2026, 8, 29, 5, tzinfo=timezone.utc)),
            AssistantMessage(text="取得新进展", message_id="assistant-work", created_at=datetime(2026, 8, 29, 5, 10, tzinfo=timezone.utc)),
            HumanMessage(text="最新消息", message_id="human-latest", created_at=datetime(2026, 8, 29, 6, tzinfo=timezone.utc)),
        )
        client = _SummaryClient()
        coordinator = ContextCompactionCoordinator(
            ContextBudgetService(ContextStrategyConfig()),
            client,
            chat_settings=_summary_settings(),
        )
        return await coordinator.summarize_candidate(frozen, 1.0), client

    candidate, client = asyncio.run(scenario())
    summary = next(message for message in candidate if isinstance(message, ContextSummaryMessage))

    assert summary.covered_through_message_id == "assistant-work"
    assert summary.covered_effective_date == "2026-08-29"
    assert "最新消息" not in str(client.calls[0]["messages"])
    assert len([message for message in candidate if isinstance(message, ContextSummaryMessage)]) == 1


def test_summary_coverage_reuses_existing_summary_cursor() -> None:
    async def scenario():
        frozen = (
            ContextSummaryMessage(
                content="旧的摘要", message_id="summary",
                summary_schema_version=1, covered_through_message_id="legacy-summary",
                covered_effective_date="2026-08-28", compression_ratio=0.5,
                created_at=datetime(2026, 8, 29, tzinfo=timezone.utc),
            ),
            HumanMessage(text="最新消息", message_id="human-latest", created_at=datetime(2026, 8, 29, 6, tzinfo=timezone.utc)),
        )
        coordinator = ContextCompactionCoordinator(
            ContextBudgetService(ContextStrategyConfig()),
            _SummaryClient(),
            chat_settings=_summary_settings(),
        )
        return await coordinator.summarize_candidate(frozen, 1.0)

    candidate = asyncio.run(scenario())
    summary = next(message for message in candidate if isinstance(message, ContextSummaryMessage))

    assert summary.covered_through_message_id == "legacy-summary"
    assert summary.covered_effective_date == "2026-08-28"


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
    frozen = (
        HumanMessage(text="旧消息", message_id="human-old", created_at=datetime(2026, 8, 28, tzinfo=timezone.utc)),
        HumanMessage(text="最新消息", message_id="human-latest", created_at=datetime(2026, 8, 29, tzinfo=timezone.utc)),
    )
    coordinator = ContextCompactionCoordinator(
        ContextBudgetService(ContextStrategyConfig()),
        client,
        chat_settings=_summary_settings(),
    )

    candidate = asyncio.run(coordinator.summarize_candidate(frozen, 1.0))

    assert len(candidate) == 2
    assert isinstance(candidate[0], ContextSummaryMessage)
    assert "中" * 800 in candidate[0].content


def test_invalid_structured_summary_does_not_replace_state_messages() -> None:
    async def scenario() -> tuple[AgentState, dict]:
        messages = []
        for index in range(12):
            messages.extend([
                HumanMessage(text=f"第{index}轮问题：" + "中" * 600, message_id=f"human-{index}", created_at=datetime(2026, 8, 29, tzinfo=timezone.utc)),
                AssistantMessage(text=f"第{index}轮回答：" + "答" * 600, message_id=f"assistant-{index}", created_at=datetime(2026, 8, 29, 0, 1, tzinfo=timezone.utc)),
            ])
        state = _state("session-invalid-summary", messages)
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
        state = _state("session-expand", (
            HumanMessage(text="old", message_id="human-old", created_at=datetime(2026, 8, 28, tzinfo=timezone.utc)),
            HumanMessage(text="latest", message_id="human-latest", created_at=datetime(2026, 8, 29, tzinfo=timezone.utc)),
        ))
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
    assert state.messages[0].text == "candidate-75"
    assert prepared.estimated_tokens == 10


def test_model_input_stops_before_main_model_when_75_percent_is_still_over_budget() -> None:
    async def scenario():
        state = _state("session-overflow", (
            HumanMessage(text="old", message_id="human-old", created_at=datetime(2026, 8, 28, tzinfo=timezone.utc)),
            HumanMessage(text="latest", message_id="human-latest", created_at=datetime(2026, 8, 29, tzinfo=timezone.utc)),
        ))
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
