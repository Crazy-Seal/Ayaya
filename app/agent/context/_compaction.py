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
    """选择需要压缩的旧消息，并返回替换后的消息集合。

    本类不写数据库，也不直接修改 AgentState；调用方确认候选结果后再更新状态。
    """

    @staticmethod
    def compress_screenshots(
        messages: Sequence[BaseMessage],
        *,
        ttl_human_messages: int,
        max_screenshots: int,
    ) -> tuple[tuple[BaseMessage, ...], int]:
        """按后续真人输入数和截图总数，把旧截图替换为保留历史文件引用的占位内容。

        Args:
            messages (Sequence[BaseMessage]): 按对话顺序排列的领域消息。
            ttl_human_messages (int): 截图后达到多少条真人消息时压缩该截图。
            max_screenshots (int): 模型上下文中最多保留多少张未压缩截图。

        Returns:
            tuple[tuple[BaseMessage, ...], int]: 替换后的消息及本次压缩的截图数量；原消息不变。
        """
        targets: set[int] = set()
        # 1. 从最新消息往前数真人输入；工具结果和后台通知不会消耗截图保留次数。
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

        # 2. 过期截图之外，仍超出数量上限时优先压缩最旧的截图。
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

        # 3. 新建截图消息只保留历史文件引用；原消息不变，由调用方提交替换结果。
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
        """缩短允许压缩的旧工具结果，并删除与这些结果明确关联的工具图片。

        Args:
            messages (Sequence[BaseMessage]): 当前对话消息。
            keep_latest (int): 保留最近多少条可压缩工具结果。
            protected_tool_call_ids (Iterable[str]): 尚未结束的工具调用编号，这些结果不得压缩。

        Returns:
            tuple[tuple[BaseMessage, ...], int]: 替换后的消息及被替换或删除的消息数量。
        """
        # 1. 尚未结束的工具调用仍需完整结果，先从可压缩候选中排除。
        protected = set(protected_tool_call_ids)
        candidates = [
            message
            for message in messages
            if isinstance(message, ToolMessage)
            and message.compressible
            and message.tool_call_id not in protected
        ]
        # 2. 最近几项保留原文；更旧的结果只有达到文本长度阈值才替换正文。
        targets = candidates[:-keep_latest] if keep_latest else candidates
        target_ids = {message.message_id for message in targets}
        changed = 0
        result: list[BaseMessage] = []
        for message in messages:
            # 3. 工具图片必须明确关联到选中结果才删除，避免误删其他调用的图片。
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
        """把模型工具请求与紧随其后的对应结果归为一组，供压缩边界检查使用。

        Args:
            messages (Sequence[BaseMessage]): 按执行顺序排列的消息。

        Returns:
            list[list[int]]: 每组消息在原序列中的下标；普通消息各自成组。
        """
        blocks: list[list[int]] = []
        # 1. 普通消息独立成组；模型的工具请求从对应结果中逐项扩展同一组。
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
        """按完整历史轮次选择待总结的前段消息，始终保留最近一次真人输入及其后内容。

        Args:
            messages (Sequence[BaseMessage]): 本次压缩开始前捕获的消息集合。
            budget_service (RoughTokenEstimator): 用于粗估各轮次大小的对象。
            ratio (float): 希望覆盖的可压缩历史 token 比例。

        Returns:
            list[int]: 从最旧轮次开始选择的消息下标；没有已完成历史时为空。

        Raises:
            ValueError: 按真人输入划分的轮次拆开了工具请求和对应结果。
        """
        # 1. 最近真人输入及其后的工具执行属于当前工作，不参与历史摘要。
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
        # 2. 按真人输入划分完整旧轮次，之后检查每轮是否包含完整工具请求/结果。
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
        # 3. 按 token 比例从最旧轮次开始选取；最后一轮整体保留，不按单条消息截断。
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
