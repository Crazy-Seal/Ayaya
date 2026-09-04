"""主模型输入预算、协议块选择和两层上下文压缩策略。"""

from __future__ import annotations

import json
import math
from copy import deepcopy
from typing import Any, Iterable

from pydantic import BaseModel, ConfigDict, Field

from app.agent.message import TOOL_IMAGE_MESSAGE_NAMES, is_real_human_message
from app.agent.message_time import RuntimeContext, format_local_time, project_messages_with_time
from app.agent.state import AgentState
from app.agent.tool_result_normalizer import weighted_char_count
from app.agent.utils.domain.text import normalize_messages_for_model


COMPRESSED_TOOL_RESULT = "[工具结果已被压缩]"
MIN_TOOL_RESULT_COMPRESSION_WEIGHT = 50
CONTEXT_SUMMARY_PREFIX = "[系统提示：前情摘要]"
CONTEXT_SUMMARY_NAME = "context_summary"


class ContextStrategyConfig(BaseModel):
    """上下文预算配置。"""

    model_config = ConfigDict(extra="forbid")

    compression_trigger_tokens_k: int = Field(default=100, ge=10)

    @property
    def compression_trigger_tokens(self) -> int:
        return self.compression_trigger_tokens_k * 1_000


def _rough_text_tokens(value: str) -> int:
    ascii_count = sum(1 for char in value if ord(char) < 128)
    return math.ceil(ascii_count / 3) + (len(value) - ascii_count)


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


class RoughTokenEstimator:
    """使用保守规则估算消息、工具定义和图片的 token 数。"""

    MESSAGE_OVERHEAD = 8
    CONTENT_PART_OVERHEAD = 4
    TOOL_CALL_OVERHEAD = 12
    IMAGE_TOKENS = 2_000

    def estimate_value(self, value: Any) -> int:
        if isinstance(value, str):
            return _rough_text_tokens(value)
        if isinstance(value, list):
            total = 0
            for item in value:
                total += self.CONTENT_PART_OVERHEAD
                if isinstance(item, dict) and item.get("type") == "image_url":
                    total += self.IMAGE_TOKENS
                else:
                    total += self.estimate_value(item)
            return total
        if isinstance(value, dict):
            return _rough_text_tokens(_stable_json(value))
        return _rough_text_tokens(str(value))

    def estimate_message(self, message: dict) -> int:
        total = self.MESSAGE_OVERHEAD + self.estimate_value(message.get("content", ""))
        for tool_call in message.get("tool_calls") or []:
            total += self.TOOL_CALL_OVERHEAD + self.estimate_value(tool_call)
        return total

    def estimate_raw(
        self,
        *,
        system_prompt: str,
        tools: list[dict] | None,
        messages: list[dict],
    ) -> int:
        total = self.MESSAGE_OVERHEAD + _rough_text_tokens(system_prompt or "")
        total += self.estimate_value(tools or [])
        total += sum(self.estimate_message(message) for message in messages)
        return max(total, 1)


class ContextBudgetService(RoughTokenEstimator):
    """估算完整主模型请求，并用真实 prompt usage 做进程内保守校准。"""

    def __init__(self, config: ContextStrategyConfig) -> None:
        self.config = config
        self._ewma_ratio = 1.0
        self.calibration_factor = 1.10

    def estimate_calibrated(self, raw_estimate: int) -> int:
        return max(raw_estimate, math.ceil(raw_estimate * self.calibration_factor))

    def calibrate(self, *, raw_estimate: int, actual_prompt_tokens: int) -> None:
        if raw_estimate <= 0 or actual_prompt_tokens <= 0:
            return
        ratio = actual_prompt_tokens / raw_estimate
        self._ewma_ratio = 0.8 * self._ewma_ratio + 0.2 * ratio
        self.calibration_factor = min(2.0, max(1.0, self._ewma_ratio * 1.10))

    def raise_after_context_error(self) -> None:
        self.calibration_factor = min(2.0, max(1.25, self.calibration_factor * 1.25))

    def should_compact(self, calibrated_estimate: int) -> bool:
        return calibrated_estimate >= self.config.compression_trigger_tokens


class ContextCompressionPolicy:
    """只负责选择和改写，不发起 LLM 请求。"""

    @staticmethod
    def compress_old_tool_results(
        messages: list[dict],
        *,
        keep_latest: int = 5,
        protected_tool_call_ids: Iterable[str] = (),
    ) -> int:
        """回收较旧的可压缩工具文本及其强关联图片。

        返回发生变更的消息数量，包括被替换的 ToolMessage 和被删除的
        工具图片 UserMessage。短文本仍参与最近结果保护，但不会被替换。
        """
        protected = set(protected_tool_call_ids)
        candidates = [
            message
            for message in messages
            if message.get("role") == "tool"
            and message.get("_compressible") is True
            and message.get("tool_call_id") not in protected
        ]
        targets = candidates[:-keep_latest] if keep_latest else candidates
        changed = 0
        target_message_ids: set[object] = set()
        for message in targets:
            message_id = message.get("_message_id")
            if message_id is not None:
                target_message_ids.add(message_id)
            content = message.get("content")
            if (
                content != COMPRESSED_TOOL_RESULT
                and weighted_char_count(str(content or ""))
                >= MIN_TOOL_RESULT_COMPRESSION_WEIGHT
            ):
                message["content"] = COMPRESSED_TOOL_RESULT
                changed += 1

        if target_message_ids:
            kept_messages: list[dict] = []
            for message in messages:
                is_associated_tool_image = (
                    message.get("role") == "user"
                    and message.get("name") in TOOL_IMAGE_MESSAGE_NAMES
                    and message.get("_associated_message_id") in target_message_ids
                )
                if is_associated_tool_image:
                    changed += 1
                else:
                    kept_messages.append(message)
            messages[:] = kept_messages
        return changed

    @staticmethod
    def protocol_blocks(messages: list[dict]) -> list[list[int]]:
        """把 assistant.tool_calls 及紧随其后的全部 ToolMessage 视为原子块。"""
        blocks: list[list[int]] = []
        index = 0
        while index < len(messages):
            message = messages[index]
            tool_calls = message.get("tool_calls") if message.get("role") == "assistant" else None
            if not tool_calls:
                blocks.append([index])
                index += 1
                continue
            expected = {call.get("id") for call in tool_calls if call.get("id")}
            block = [index]
            cursor = index + 1
            while cursor < len(messages):
                later = messages[cursor]
                if later.get("role") != "tool" or later.get("tool_call_id") not in expected:
                    break
                block.append(cursor)
                cursor += 1
            blocks.append(block)
            index = cursor
        return blocks

    @classmethod
    def select_prefix(
        cls,
        messages: list[dict],
        budget_service: RoughTokenEstimator,
        *,
        ratio: float,
    ) -> list[int]:
        latest_human_index = max(
            (index for index, message in enumerate(messages) if is_real_human_message(message)),
            default=len(messages),
        )
        eligible_indices = list(range(latest_human_index))
        if not eligible_indices:
            return []
        # 普通对话按完整真人轮次选择；工具调用仍由 protocol_blocks 校验原子性。
        units: list[list[int]] = []
        current: list[int] = []
        for index in eligible_indices:
            if is_real_human_message(messages[index]) and current:
                units.append(current)
                current = []
            current.append(index)
        if current:
            units.append(current)
        atomic_sets = [set(block) for block in cls.protocol_blocks(messages)]
        for unit in units:
            unit_set = set(unit)
            for atomic in atomic_sets:
                if unit_set & atomic and not atomic <= unit_set:
                    raise ValueError("压缩轮次边界拆分了工具协议块")
        eligible_tokens = sum(
            budget_service.estimate_message(messages[index])
            for unit in units for index in unit
        )
        target = eligible_tokens * ratio
        selected: list[int] = []
        used = 0
        for unit in units:
            selected.extend(unit)
            used += sum(budget_service.estimate_message(messages[index]) for index in unit)
            if used >= target:
                break
        return selected


TurnModelContext = RuntimeContext


def append_transient_context(
    messages: list[dict],
    turn_context: TurnModelContext,
) -> list[dict]:
    """只在模型输入副本中给最新真人消息追加时间和召回记忆。"""
    result = deepcopy(messages)
    target = next(
        (
            message for message in reversed(result)
            if message.get("_message_id") == turn_context.latest_human_message_id
            and is_real_human_message(message)
        ),
        None,
    )
    if target is None:
        target = next((m for m in reversed(result) if is_real_human_message(m)), None)
    if target is None:
        return result
    suffix = f"[系统提示]\n当前日期时间：{format_local_time(turn_context.captured_at)}"
    if turn_context.recalled_memory.strip():
        suffix += (
            "\n\n以下是自动检索到的可能相关记忆，仅供参考：\n"
            + turn_context.recalled_memory.strip()
        )
    content = target.get("content", "")
    if isinstance(content, list):
        target["content"] = [*content, {"type": "text", "text": suffix}]
    else:
        target["content"] = f"{content}\n\n{suffix}" if content else suffix
    return result


class ContextStrategyManager:
    """组合上下文预算、压缩选择策略与模型消息投影。"""

    def __init__(self, config: ContextStrategyConfig) -> None:
        self.config = config
        self.budget = ContextBudgetService(config)
        self.policy = ContextCompressionPolicy()

    def build_model_window(self, state: AgentState | list[dict]) -> list[dict]:
        messages = state.messages if isinstance(state, AgentState) else state
        return normalize_messages_for_model(project_messages_with_time(deepcopy(messages)))
