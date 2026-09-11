"""上下文压缩的纯选择策略。"""

from collections.abc import Iterable

from app.agent.state.message_names import (
    CONTEXT_SUMMARY_MESSAGE_NAME,
    TOOL_IMAGE_MESSAGE_NAMES,
)
from app.agent.state.messages import is_real_human_message
from app.agent.utils.domain.token_estimation import RoughTokenEstimator
from app.agent.utils.domain.weighted_text import weighted_char_count


COMPRESSED_TOOL_RESULT = "[工具结果已被压缩]"
MIN_TOOL_RESULT_COMPRESSION_WEIGHT = 50
CONTEXT_SUMMARY_PREFIX = "[系统提示：前情摘要]"
CONTEXT_SUMMARY_NAME = CONTEXT_SUMMARY_MESSAGE_NAME


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
            tool_calls = (
                message.get("tool_calls")
                if message.get("role") == "assistant"
                else None
            )
            if not tool_calls:
                blocks.append([index])
                index += 1
                continue
            expected = {call.get("id") for call in tool_calls if call.get("id")}
            block = [index]
            cursor = index + 1
            while cursor < len(messages):
                later = messages[cursor]
                if (
                    later.get("role") != "tool"
                    or later.get("tool_call_id") not in expected
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
        messages: list[dict],
        budget_service: RoughTokenEstimator,
        *,
        ratio: float,
    ) -> list[int]:
        latest_human_index = max(
            (
                index
                for index, message in enumerate(messages)
                if is_real_human_message(message)
            ),
            default=len(messages),
        )
        eligible_indices = list(range(latest_human_index))
        if not eligible_indices:
            return []
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
            for unit in units
            for index in unit
        )
        target = eligible_tokens * ratio
        selected: list[int] = []
        used = 0
        for unit in units:
            selected.extend(unit)
            used += sum(
                budget_service.estimate_message(messages[index]) for index in unit
            )
            if used >= target:
                break
        return selected


__all__ = [
    "COMPRESSED_TOOL_RESULT",
    "CONTEXT_SUMMARY_NAME",
    "CONTEXT_SUMMARY_PREFIX",
    "ContextCompressionPolicy",
]
