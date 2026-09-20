"""Agent 上下文的纯选择与不可变替换策略。"""

from collections.abc import Iterable, Sequence
from dataclasses import replace

from app.agent.state.messages import (
    AssistantMessage,
    BaseMessage,
    CompactedMedia,
    HumanMessage,
    ToolMediaMessage,
    ToolMessage,
)
from app.agent.utils.domain.token_estimation import RoughTokenEstimator
from app.agent.utils.domain.weighted_text import weighted_char_count


COMPRESSED_TOOL_RESULT = "[工具结果已被压缩]"
COMPRESSED_SCREENSHOT_PLACEHOLDER = "[系统消息]已被压缩的旧截图"
MIN_TOOL_RESULT_COMPRESSION_WEIGHT = 50
CONTEXT_SUMMARY_PREFIX = "[系统提示：前情摘要]"


class ContextCompressionPolicy:
    """在不修改输入消息值的前提下选择 replacement。"""

    @staticmethod
    def compress_screenshots(
        messages: Sequence[BaseMessage],
        *,
        ttl_human_messages: int,
        max_screenshots: int,
    ) -> tuple[tuple[BaseMessage, ...], int]:
        targets: set[int] = set()
        human_count_after = 0
        for index in range(len(messages) - 1, -1, -1):
            message = messages[index]
            if (
                isinstance(message, ToolMediaMessage)
                and message.media_kind == "screenshot"
                and not isinstance(message.payload, CompactedMedia)
                and human_count_after >= ttl_human_messages
            ):
                targets.add(index)
            if isinstance(message, HumanMessage):
                human_count_after += 1

        remaining = [
            index
            for index, message in enumerate(messages)
            if index not in targets
            and isinstance(message, ToolMediaMessage)
            and message.media_kind == "screenshot"
            and not isinstance(message.payload, CompactedMedia)
        ]
        excess = max(len(remaining) - max_screenshots, 0)
        targets.update(remaining[:excess])

        candidate = list(messages)
        for index in targets:
            message = candidate[index]
            assert isinstance(message, ToolMediaMessage)
            candidate[index] = replace(
                message,
                payload=CompactedMedia(storage_refs=message.payload.storage_refs),
            )
        return tuple(candidate), len(targets)

    @staticmethod
    def compress_old_tool_results(
        messages: Sequence[BaseMessage],
        *,
        keep_latest: int = 5,
        protected_tool_call_ids: Iterable[str] = (),
    ) -> tuple[tuple[BaseMessage, ...], int]:
        protected = set(protected_tool_call_ids)
        candidates = [
            message
            for message in messages
            if isinstance(message, ToolMessage)
            and message.compressible
            and message.tool_call_id not in protected
        ]
        targets = candidates[:-keep_latest] if keep_latest else candidates
        target_ids = {message.message_id for message in targets}
        changed = 0
        result: list[BaseMessage] = []
        for message in messages:
            if (
                isinstance(message, ToolMediaMessage)
                and message.associated_message_id in target_ids
            ):
                changed += 1
                continue
            if (
                isinstance(message, ToolMessage)
                and message.message_id in target_ids
                and message.content != COMPRESSED_TOOL_RESULT
                and weighted_char_count(message.content)
                >= MIN_TOOL_RESULT_COMPRESSION_WEIGHT
            ):
                result.append(replace(message, content=COMPRESSED_TOOL_RESULT))
                changed += 1
            else:
                result.append(message)
        return tuple(result), changed

    @staticmethod
    def protocol_blocks(messages: Sequence[BaseMessage]) -> list[list[int]]:
        blocks: list[list[int]] = []
        index = 0
        while index < len(messages):
            message = messages[index]
            if not isinstance(message, AssistantMessage) or not message.tool_calls:
                blocks.append([index])
                index += 1
                continue
            expected = {call.id for call in message.tool_calls}
            block = [index]
            cursor = index + 1
            while cursor < len(messages):
                later = messages[cursor]
                if (
                    not isinstance(later, ToolMessage)
                    or later.tool_call_id not in expected
                ):
                    break
                block.append(cursor)
                cursor += 1
            blocks.append(block)
            index = cursor
        return blocks

    @classmethod
    def select_prefix(
        cls,
        messages: Sequence[BaseMessage],
        budget_service: RoughTokenEstimator,
        *,
        ratio: float,
    ) -> list[int]:
        latest_human_index = max(
            (
                index
                for index, message in enumerate(messages)
                if isinstance(message, HumanMessage)
            ),
            default=len(messages),
        )
        eligible_indices = list(range(latest_human_index))
        if not eligible_indices:
            return []
        units: list[list[int]] = []
        current: list[int] = []
        for index in eligible_indices:
            if isinstance(messages[index], HumanMessage) and current:
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
            budget_service.estimate_message(messages[index].to_openai_format())
            for unit in units
            for index in unit
        )
        target = eligible_tokens * ratio
        selected: list[int] = []
        used = 0
        for unit in units:
            selected.extend(unit)
            used += sum(
                budget_service.estimate_message(messages[index].to_openai_format())
                for index in unit
            )
            if used >= target:
                break
        return selected


__all__ = [
    "COMPRESSED_SCREENSHOT_PLACEHOLDER",
    "COMPRESSED_TOOL_RESULT",
    "CONTEXT_SUMMARY_PREFIX",
    "ContextCompressionPolicy",
]
