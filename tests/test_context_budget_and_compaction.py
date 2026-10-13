from app.agent.models.openai_message_names import CONTEXT_SUMMARY_MESSAGE_NAME, MCP_TOOL_IMAGE_MESSAGE_NAME, SCREENSHOT_COMPRESSED_NAME, SCREENSHOT_MESSAGE_NAME
from tests.model_capture import capture_input
from app.agent.state.messages import text_content
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
from app.agent.state.messages import (AssistantMessage, ImageContent, ContextSummaryMessage, HumanMessage, SystemMessage, ToolCall, ToolMessage)
from app.agent.context.coordinator import (
    CONTEXT_SUMMARY_SCHEMA,
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
    """构造摘要生成使用的角色与称呼配置。

    Args:
        address (str): 摘要提示词使用的用户称呼。

    Returns:
        SimpleNamespace: 供外层用例断言使用的预设结果。
    """
    return SimpleNamespace(
        name="Ayaya",
        feature="",
        character="助手",
        address=address,
        characteristic="",
    )


def _tool_call(message_id: str, tool_call_id: str) -> AssistantMessage:
    """构造带稳定编号和参数的测试工具调用。

    Args:
        message_id (str): 原领域消息的稳定编号。
        tool_call_id (str): 用于配对工具请求和结果的调用编号。

    Returns:
        AssistantMessage: 供外层用例断言使用的预设结果。
    """
    return AssistantMessage(
        message_id=message_id,
        created_at=datetime(2026, 8, 29, tzinfo=timezone.utc),
        content=text_content(""),
        tool_calls=(ToolCall(id=tool_call_id, name="read_file", args={}),),
    )


def _tool_result(
    index: int,
    *,
    compressible: bool = True,
    content: str | None = None,
) -> ToolMessage:
    """构造与指定工具调用配对的文本结果。

    Args:
        index (int): 模型片段中的工具调用索引。
        compressible (bool): 是否允许后续上下文压缩缩短这条工具结果。
        content (str | None): 当前处理的正文。

    Returns:
        ToolMessage: 供外层用例断言使用的预设结果。
    """
    return ToolMessage(
        message_id=f"tool-{index}",
        created_at=datetime(2026, 8, 29, 0, 0, 1, tzinfo=timezone.utc),
        tool_name="read_file",
        tool_call_id=f"call-{index}",
        content=text_content(content if content is not None else f"result-{index}"),
        compressible=compressible,
    )




def _human(index: int) -> HumanMessage:
    """构造当前用例的真人输入消息。

    Args:
        index (int): 模型片段中的工具调用索引。

    Returns:
        HumanMessage: 供外层用例断言使用的预设结果。
    """
    return HumanMessage(
        message_id=f"human-{index}",
        created_at=datetime(2026, 8, 29, 0, 0, index, tzinfo=timezone.utc),
        content=text_content(f"human-{index}"),
    )


def _state(session_id: str, messages) -> AgentState:
    """按用例给定消息和未完成调用构造会话状态。

    Args:
        session_id (str): 当前操作所属的会话编号。
        messages (list[dict]): 按对话顺序排列的消息集合。

    Returns:
        AgentState: 供外层用例断言使用的预设结果。
    """
    state = AgentState.create_new(session_id)
    checkpoint = state.to_checkpoint()
    checkpoint["messages"] = [message.to_storage_format() for message in messages]
    return AgentState.from_checkpoint(checkpoint)


def test_screenshot_policy_expires_group_without_rehydrating_files() -> None:
    """TTL 只计真人输入，同一结果图片共同压缩并保留历史引用。"""
    result = _tool_result(0)
    result = replace(result, content=(*result.content, ImageContent("data:image/png;base64,AAAA", media_kind="screenshot", storage_ref="screenshot/a.png")))
    messages = (result, _human(1), _human(2))
    candidate, changed = ContextCompressionPolicy.compress_screenshots(messages, ttl_human_messages=2, max_screenshots=2)
    assert changed == 1 and candidate[0].content[-1].context_state == "compacted"
    assert candidate[0].content[-1].url == "" and candidate[0].content[-1].storage_ref == "screenshot/a.png"
    assert result.content[-1].url.startswith("data:")
    assert ContextCompressionPolicy.compress_screenshots(candidate, ttl_human_messages=2, max_screenshots=2)[1] == 0


def test_screenshot_policy_counts_results_and_keeps_latest_two() -> None:
    """同一工具返回多图仍是一个观察，保留最近两组截图。"""
    messages = [replace(_tool_result(i), content=(*text_content("图"), *(ImageContent("data:image/png;base64,AAAA", media_kind="screenshot") for _ in range(2)))) for i in range(3)]
    candidate, changed = ContextCompressionPolicy.compress_screenshots(messages, ttl_human_messages=20, max_screenshots=2)
    assert changed == 1
    assert all(part.context_state == "compacted" for part in candidate[0].content if isinstance(part, ImageContent))
    assert candidate[-1].content[-1].context_state == "available"


def test_first_layer_protects_pending_batch_and_keeps_latest_five() -> None:
    """未闭合批次与最近五项旧观察保持完整，其余长正文可压缩。"""
    messages = [replace(_tool_result(i, content="x" * 300), content=(*text_content("x" * 300), ImageContent("data:image/png;base64,AAAA", media_kind="tool"))) for i in range(7)]
    candidate, changed = ContextCompressionPolicy.compress_old_tool_results(messages, protected_tool_call_ids=("call-0",))
    assert changed == 1 and candidate[0] == messages[0]
    assert candidate[1].text == COMPRESSED_TOOL_RESULT and len(candidate[1].content) == 1
    assert candidate[-1] == messages[-1]


@pytest.mark.parametrize("text,expected", [("a" * 200, COMPRESSED_TOOL_RESULT), ("短文", "短文")])
def test_first_layer_preserves_short_text_and_removes_only_own_images(text, expected) -> None:
    """沿用文本权重阈值，图片与所属结果一起回收。"""
    result = replace(_tool_result(0), content=(*text_content(text), ImageContent("data:image/png;base64,AAAA", media_kind="tool")))
    candidate, changed = ContextCompressionPolicy.compress_old_tool_results((result,), keep_latest=0)
    assert changed == 1 and candidate[0].text == expected
    assert len(candidate[0].content) == 1


def test_screen_tools_allow_old_results_to_be_compacted() -> None:
    """截图和屏幕工具仍标记为可压缩旧观察。"""
    assert screenshot().compressible and control_screen().compressible and control_screen_with_vlm().compressible


def test_diary_coverage_port_removes_only_covered_context_summaries() -> None:
    """验证只删除已被日记覆盖的上下文摘要。"""
    class Coverage:
        """提供当前用例所需的 Coverage 测试替身，执行记录仅供外层断言使用。

        该定义仅用于外层用例：验证只删除已被日记覆盖的上下文摘要。
        """
        async def has_diary_for_effective_date(self, effective_date: str) -> bool:
            """按用例指定的日期判断日记覆盖，不访问真实日记。

            该定义仅用于外层用例：验证只删除已被日记覆盖的上下文摘要。

            Args:
                effective_date (str): 按日记规则划分的有效日期，格式为 YYYY-MM-DD。

            Returns:
                bool: 供外层用例断言使用的预设结果。
            """
            return effective_date == "2026-08-28"

    async def scenario() -> tuple[int, list[str | None]]:
        """由外层测试调用，完成“只删除已被日记覆盖的上下文摘要”的异步步骤。

        Returns:
            tuple[int, list[str | None]]: 供外层用例断言使用的预设结果。
        """
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
    """验证预算使用真实输入用量加粗估净变化。"""
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
    """验证预算净变化包含消息替换和删除。"""
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
    """验证不兼容的真实用量基线被丢弃。

    Args:
        accounting_key (str): 用例指定的模型计量标识。
        system_prompt (str): 本次模型调用的系统提示词。
    """
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
    """验证工具定义变化导致真实用量基线失效。"""
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
    """验证缺少真实用量时不更新基线。"""
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
    """验证真实用量基线经 checkpoint 保存读取仍完整。"""
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
    """验证压缩阈值按千 token 配置直接换算。"""
    defaults = ContextStrategyConfig()
    minimum = ContextStrategyConfig(compression_trigger_tokens_k=10)

    assert defaults.compression_trigger_tokens == 100_000
    assert minimum.compression_trigger_tokens == 10_000
    with pytest.raises(ValidationError):
        ContextStrategyConfig(compression_trigger_tokens_k=9)


def test_protocol_blocks_never_split_tool_call_from_results() -> None:
    """验证工具请求和结果不会被压缩边界拆开。"""
    messages = [
        HumanMessage(content=text_content("one")),
        _tool_call("ai-1", "call-1"),
        _tool_result(1),
        AssistantMessage(content=text_content("done")),
        HumanMessage(content=text_content("latest")),
    ]

    blocks = ContextCompressionPolicy.protocol_blocks(messages)

    assert any(block == [1, 2] for block in blocks)


def test_transient_context_is_appended_to_copy_of_latest_human_message() -> None:
    """验证临时背景只追加到最新真人消息副本。"""
    original = (HumanMessage(
        content=text_content("问题"),
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
    """提供按用例脚本输出的模型响应，验证流式处理、摘要或失败恢复，不调用真实模型。"""
    def __init__(self, result: dict | None = None) -> None:
        """保存本用例的脚本输入和依赖，初始化测试对象的执行记录。

        Args:
            result (dict | None): 当前工具或被包装函数返回的结果。
        """
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
        """接收摘要请求并返回预设结构或异常，供检查候选校验及恢复次数。

        Args:
            messages (list[dict]): 按对话顺序排列的消息集合。
            schema (dict): 当前要检查或转换的 JSON Schema。
            kwargs (dict[str, Any]): 传递给被调用函数的其他关键字参数。
        """
        messages = capture_input(messages)
        self.calls.append({"messages": messages, "schema": schema, "kwargs": kwargs})
        return self.result


def test_context_summary_schema_normalizes_and_deduplicates_task_lists() -> None:
    """验证摘要任务列表经过规范化和去重。"""
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
    """验证无效摘要结构被拒绝。

    Args:
        payload (dict): 用例指定的结构化模型响应或调用载荷。
    """
    with pytest.raises(ValidationError):
        ContextSummary.model_validate(payload)


class _RatioBudget:
    """让60%候选仍超限、75%候选低于阈值的可预测预算替身。"""

    def __init__(self, config: ContextStrategyConfig, *, accept_75: bool = True) -> None:
        """保存本用例的脚本输入和依赖，初始化测试对象的执行记录。

        该定义仅用于外层用例：让60%候选仍超限、75%候选低于阈值的可预测预算替身。

        Args:
            config (ContextStrategyConfig): 调用方提供的配置。
            accept_75 (bool): 是否让百分之七十五摘要候选通过预算检查。
        """
        self.config = config
        self.accept_75 = accept_75

    def assess(self, *, messages: list[dict], **_kwargs) -> TokenAssessment:
        """返回用例指定的 token 估算结果，控制是否需要第二次摘要。

        该定义仅用于外层用例：让60%候选仍超限、75%候选低于阈值的可预测预算替身。

        Args:
            messages (list[dict]): 按对话顺序排列的消息集合。
            _kwargs (dict[str, Any]): 传递给被调用函数的其他关键字参数。

        Returns:
            TokenAssessment: 供外层用例断言使用的预设结果。
        """
        marker = " ".join(message.text for message in messages)
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
        """按测试预算规则判断当前候选是否应继续压缩。

        该定义仅用于外层用例：让60%候选仍超限、75%候选低于阈值的可预测预算替身。

        Args:
            estimate (int): 测试指定的输入 token 估算值。

        Returns:
            bool: 供外层用例断言使用的预设结果。
        """
        return estimate >= self.config.compression_trigger_tokens


class _RatioCompaction:
    """提供当前用例所需的 _RatioCompaction 测试替身，执行记录仅供外层断言使用。"""
    def __init__(self) -> None:
        """保存本用例的脚本输入和依赖，初始化测试对象的执行记录。"""
        self.ratios: list[float] = []

    @staticmethod
    def compress_tool_results(_state: AgentState) -> int:
        """记录第一层工具结果压缩调用，供检查恢复顺序。

        Args:
            _state (AgentState): 当前会话的内存 AgentState；保存由调用方负责。

        Returns:
            int: 供外层用例断言使用的预设结果。
        """
        return 0

    @staticmethod
    async def drop_diary_covered_summaries(_state: AgentState) -> int:
        """记录日记覆盖清理调用，避免访问真实日记服务。

        Args:
            _state (AgentState): 当前会话的内存 AgentState；保存由调用方负责。

        Returns:
            int: 供外层用例断言使用的预设结果。
        """
        return 0

    async def summarize_candidate(self, frozen, ratio: float):
        """按用例要求构造摘要候选，记录原历史与比例供外层断言。

        Args:
            frozen (Any): 压缩开始时捕获的原历史，用于检查两次候选使用同一来源。
            ratio (float): 本次摘要选择的历史比例。
        """
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
    """验证旧轮次被仅保存到状态的摘要替换。"""
    async def scenario():
        """由外层测试调用，完成“旧轮次被仅保存到状态的摘要替换”的异步步骤。"""
        messages = []
        created_at = datetime(2026, 8, 29, tzinfo=timezone.utc)
        for index in range(12):
            messages.extend([
                HumanMessage(
                    content=text_content(f"第{index}轮问题：" + "中" * 600),
                    message_id=f"human-{index}", created_at=created_at,
                ),
                AssistantMessage(
                    content=text_content(f"第{index}轮回答：" + "答" * 600),
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
    assert any("仅存在于本轮输入的召回记忆" in message.text for message in prepared.messages)
    assert prepared.estimated_tokens < config.compression_trigger_tokens


def test_context_summary_omits_none_values_from_optional_chat_settings() -> None:
    """验证空角色设置不会将 None 文本发送给摘要模型。"""
    async def scenario() -> list[dict]:
        """由外层测试调用，完成“空角色设置不会将 None 文本发送给摘要模型”的异步步骤。

        Returns:
            list[dict]: 供外层用例断言使用的预设结果。
        """
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
            HumanMessage(content=text_content("旧消息"), message_id="human-old", created_at=datetime(2026, 8, 28, tzinfo=timezone.utc)),
            HumanMessage(content=text_content("最新消息"), message_id="human-latest", created_at=datetime(2026, 8, 29, tzinfo=timezone.utc)),
        )
        await coordinator.summarize_candidate(frozen, 1.0)
        return client.calls[0]["messages"]

    messages = asyncio.run(scenario())
    system_prompt = messages[0]["content"]

    assert "None" not in system_prompt


def test_repeated_compaction_advances_coverage_from_old_summary_to_latest_raw_message() -> None:
    """验证重复压缩的覆盖边界推进到新原消息。"""
    async def scenario():
        """由外层测试调用，完成“重复压缩的覆盖边界推进到新原消息”的异步步骤。"""
        old_payload = {"active_tasks": [], "summary": "旧摘要"}
        frozen = (
            ContextSummaryMessage(
                content="[系统提示：前情摘要]\n" + json.dumps(old_payload, ensure_ascii=False),
                message_id="summary-old", summary_schema_version=1,
                covered_through_message_id="assistant-original",
                covered_effective_date="2026-08-28", compression_ratio=0.5,
                created_at=datetime(2026, 8, 29, tzinfo=timezone.utc),
            ),
            HumanMessage(content=text_content("继续处理"), message_id="human-work", created_at=datetime(2026, 8, 29, 5, tzinfo=timezone.utc)),
            AssistantMessage(content=text_content("取得新进展"), message_id="assistant-work", created_at=datetime(2026, 8, 29, 5, 10, tzinfo=timezone.utc)),
            HumanMessage(content=text_content("最新消息"), message_id="human-latest", created_at=datetime(2026, 8, 29, 6, tzinfo=timezone.utc)),
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
    """验证摘要末尾为旧摘要时复用原覆盖游标。"""
    async def scenario():
        """由外层测试调用，完成“摘要末尾为旧摘要时复用原覆盖游标”的异步步骤。"""
        frozen = (
            ContextSummaryMessage(
                content="旧的摘要", message_id="summary",
                summary_schema_version=1, covered_through_message_id="legacy-summary",
                covered_effective_date="2026-08-28", compression_ratio=0.5,
                created_at=datetime(2026, 8, 29, tzinfo=timezone.utc),
            ),
            HumanMessage(content=text_content("最新消息"), message_id="human-latest", created_at=datetime(2026, 8, 29, 6, tzinfo=timezone.utc)),
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
    """验证结构合法摘要不会仅因粗估大小被拒绝。"""
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
        HumanMessage(content=text_content("旧消息"), message_id="human-old", created_at=datetime(2026, 8, 28, tzinfo=timezone.utc)),
        HumanMessage(content=text_content("最新消息"), message_id="human-latest", created_at=datetime(2026, 8, 29, tzinfo=timezone.utc)),
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
    """验证无效结构化摘要不替换状态消息。"""
    async def scenario() -> tuple[AgentState, dict]:
        """由外层测试调用，完成“无效结构化摘要不替换状态消息”的异步步骤。

        Returns:
            tuple[AgentState, dict]: 供外层用例断言使用的预设结果。
        """
        messages = []
        for index in range(12):
            messages.extend([
                HumanMessage(content=text_content(f"第{index}轮问题：" + "中" * 600), message_id=f"human-{index}", created_at=datetime(2026, 8, 29, tzinfo=timezone.utc)),
                AssistantMessage(content=text_content(f"第{index}轮回答：" + "答" * 600), message_id=f"assistant-{index}", created_at=datetime(2026, 8, 29, 0, 1, tzinfo=timezone.utc)),
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
    """验证第二次压缩从同一历史扩大到百分之七十五。"""
    async def scenario(*, accept_75: bool):
        """由外层测试调用，完成“第二次压缩从同一历史扩大到百分之七十五”的异步步骤。

        Args:
            accept_75 (bool): 是否让百分之七十五摘要候选通过预算检查。
        """
        state = _state("session-expand", (
            HumanMessage(content=text_content("old"), message_id="human-old", created_at=datetime(2026, 8, 28, tzinfo=timezone.utc)),
            HumanMessage(content=text_content("latest"), message_id="human-latest", created_at=datetime(2026, 8, 29, tzinfo=timezone.utc)),
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
    """验证百分之七十五摘要仍过大时停止主模型调用。"""
    async def scenario():
        """由外层测试调用，完成“百分之七十五摘要仍过大时停止主模型调用”的异步步骤。"""
        state = _state("session-overflow", (
            HumanMessage(content=text_content("old"), message_id="human-old", created_at=datetime(2026, 8, 28, tzinfo=timezone.utc)),
            HumanMessage(content=text_content("latest"), message_id="human-latest", created_at=datetime(2026, 8, 29, tzinfo=timezone.utc)),
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
