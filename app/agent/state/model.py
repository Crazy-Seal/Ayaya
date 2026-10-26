"""通过语义消息 Interface 管理可恢复的 Agent 状态。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from app.schemas.runtime_types import DESKTOP_CHANNEL, ApprovalDecisionKind, AuthorizationResolutionKind, InterruptKind
from app.schemas.message_types import MediaKind
from app.agent.state.messages import (
    AssistantMessage,
    BaseMessage,
    ContentPart, ImageContent, ProviderData, text_content,
    ContextSummaryMessage,
    EnvironmentPerceptionMessage,
    HumanMessage,
    RuntimeInputMessage,
    SystemMessage,
    ToolCall,
    ToolMessage,
    message_from_storage_format,
    to_json_value,
)
from app.agent.state.time import normalize_utc, utc_now


CHECKPOINT_SCHEMA_VERSION = 3


class TokenBaseline(BaseModel):
    """一次成功主模型请求留下的真实输入 token 锚点。"""

    accounting_key: str
    estimator_version: int
    static_fingerprint: str
    rough_prompt_tokens: int = Field(gt=0)
    actual_prompt_tokens: int = Field(gt=0)


class TemporaryContextItem(BaseModel):
    """保存本轮提供给模型的一项临时背景，随 checkpoint 恢复。

    Attributes:
        title (str): 模型可读的背景标题。
        text (str): 框架或插件整理好的背景正文。
        images (tuple[ImageContent, ...]): 正文对应的环境图片，使用 MediaKind.ENVIRONMENT 标记来源，随本轮 checkpoint 保存。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str
    text: str = ""
    images: tuple[ImageContent, ...] = ()


class AgentState(BaseModel):
    """Agent 可恢复状态；消息集合只能通过语义命令修改。

    当前审批的首次决定和系统取消由本类业务方法记录，保留工具恢复材料。
    这些方法只更新内存；Scheduler 继续负责锁、checkpoint 保存和事件发布。

    Attributes:
        session_id (str): 共享 Ayaya 会话标识。
        token_baseline (TokenBaseline | None): 最近一次已观测的模型输入 token 基线。
        long_term_memory_turn_counter (int): 已经持久提交的真人轮次计数。
        pending_tool_calls (tuple[ToolCall, ...]): Assistant 步骤中尚未闭合结果的工具调用。
        interrupt_data (dict[str, Any] | None): 持久中断路由、单次授权及恢复决定。
        pending_actions (tuple[ToolCall, ...]): 当前中断之后仍未执行的同批工具调用。
        extra (dict[str, Any]): 插件与工具的领域附加状态。
        temporary_context (dict[str, TemporaryContextItem]): 以来源和条目标识索引的本轮临时背景，随 checkpoint 保存。
        created_at (datetime): 原消息 UTC 创建时间。
        updated_at (datetime): 状态最近修改的 UTC 时间。
        _messages (list[BaseMessage]): 当前会话的内存消息序列，只能经追加、结果记录或压缩替换方法修改。
    """

    session_id: str
    token_baseline: TokenBaseline | None = None
    long_term_memory_turn_counter: int = 0
    pending_tool_calls: tuple[ToolCall, ...] = ()
    interrupt_data: dict[str, Any] | None = None
    pending_actions: tuple[ToolCall, ...] = ()
    extra: dict[str, Any] = Field(default_factory=dict)
    temporary_context: dict[str, TemporaryContextItem] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    _messages: list[BaseMessage] = PrivateAttr(default_factory=list)

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    @property
    def messages(self) -> tuple[BaseMessage, ...]:
        """提供不可变消息序列快照，外部代码不能直接增删内部消息列表。"""
        return tuple(self._messages)


    def append_input(self, message: HumanMessage | RuntimeInputMessage) -> None:
        """把真人消息或运行时通知追加到内存消息序列，不保存数据库。

        Args:
            message (HumanMessage | RuntimeInputMessage): 当前需要读取或追加的消息。

        Raises:
            TypeError: 输入不是真人消息或运行时通知。
            ValueError: 消息编号已存在。
        """
        if not isinstance(message, (HumanMessage, RuntimeInputMessage)):
            raise TypeError("Agent 输入必须是 HumanMessage 或 RuntimeInputMessage")
        self._append(message)

    def add_user_message(
        self,
        text: str,
        *,
        image_urls: Sequence[str] = (),
        image_filenames: Sequence[str] = (),
        origin_channel: str = DESKTOP_CHANNEL,
    ) -> HumanMessage:
        """构造带来源的真人消息并追加到经过协议校验的输入序列。

        Args:
            text (str): 需要保存或追加的文本。
            image_urls (Sequence[str]): 真人附件的模型图片地址。
            image_filenames (Sequence[str]): 已外置保存的附件路径。
            origin_channel (str): 输入或本轮回复所属的渠道。

        Returns:
            HumanMessage: 已追加到内存的真人消息，尚未保存 checkpoint。
        """
        if image_filenames and len(image_filenames) != len(image_urls):
            raise ValueError("真人图片与本地引用数量不一致")
        message = HumanMessage(
            content=(*text_content(text), *(ImageContent(url, storage_ref=image_filenames[index] if image_filenames else None)
                for index, url in enumerate(image_urls))), origin_channel=origin_channel,
        )
        self.append_input(message)
        return message

    def record_assistant_step(
        self,
        content: str,
        tool_calls: Sequence[ToolCall] = (),
        *,
        origin_channel: str = DESKTOP_CHANNEL,
        provider_data: ProviderData | None = None,
    ) -> AssistantMessage:
        """记录带来源的 Assistant 步骤，上一批工具未闭合时禁止开始下一批。

        Args:
            content (str): Assistant 正文。
            tool_calls (Sequence[ToolCall]): 本步模型请求的工具调用。
            origin_channel (str): 输入或本轮回复所属的渠道。

        Returns:
            AssistantMessage: 已追加的助手消息，工具请求同时登记到未完成批次。

        Raises:
            ValueError: 上一批工具调用尚未闭合，不能记录新的 assistant step。
        """
        calls = tuple(tool_calls)
        if self.pending_tool_calls:
            raise ValueError("上一批工具调用尚未闭合，不能记录新的 assistant step")
        message = AssistantMessage(content=text_content(content), tool_calls=calls, origin_channel=origin_channel, provider_data=provider_data)
        self._append(message)
        self.pending_tool_calls = calls
        return message

    def add_assistant_message(self, content: str, *, origin_channel: str = DESKTOP_CHANNEL) -> AssistantMessage:
        """通过统一 Assistant 步骤入口记录纯文本回复及来源。

        Args:
            content (str): Assistant 正文。
            origin_channel (str): 输入或本轮回复所属的渠道。

        Returns:
            AssistantMessage: 通过统一入口追加的纯文本助手消息。
        """
        return self.record_assistant_step(content, origin_channel=origin_channel)

    def record_tool_result(
        self, tool_call: ToolCall, content: Sequence[ContentPart], *, compressible: bool = False,
    ) -> ToolMessage:
        """立即登记完整工具结果，再移除待处理调用；调用方随后持久化。

        Args:
            tool_call (ToolCall): 当前未完成批次中的调用。
            content (Sequence[ContentPart]): 已校验的文字与图片，按工具返回顺序排列。
            compressible (bool): 是否允许后续压缩旧观察。
        Returns:
            ToolMessage: 已加入内存的完整结果，尚未保存数据库。
        Raises:
            ValueError: 调用不在当前批次或工具名不匹配。
        """
        # 1. 先校验请求归属，禁止把一个工具的结果用于闭合另一个调用。
        pending = next((call for call in self.pending_tool_calls if call.id == tool_call.id), None)
        if pending is None or pending.name != tool_call.name:
            raise ValueError(f"工具调用未处于待闭合批次或名称不匹配: {tool_call.id}")
        message = ToolMessage(content=tuple(content), tool_name=pending.name,
                              tool_call_id=pending.id, compressible=compressible)
        # 2. 文字和图片已在同一条消息中；保存失败时持久 pending 状态仍可恢复。
        self._append(message)
        self.pending_tool_calls = tuple(call for call in self.pending_tool_calls if call.id != pending.id)
        return message


    def add_tool_message(
        self,
        content: str,
        tool_name: str,
        tool_call_id: str,
        *,
        compressible: bool = False,
    ) -> ToolMessage:
        """恢复或取消流程按已保存调用编号登记合成结果，仍必须属于未闭合批次。

        Args:
            content (str): 当前处理的正文。
            tool_name (str): 注册表中使用的工具名称。
            tool_call_id (str): 用于配对工具请求和结果的调用编号。
            compressible (bool): 是否允许后续上下文压缩缩短这条工具结果。
        """
        call = next(
            (
                candidate for candidate in self.pending_tool_calls
                if candidate.id == tool_call_id and candidate.name == tool_name
            ),
            None,
        )
        if call is None:
            raise ValueError(f"工具调用未处于待闭合批次: {tool_call_id}")
        return self.record_tool_result(call, text_content(content), compressible=compressible)

    def set_pending_tool_calls(self, tool_calls: Sequence[ToolCall]) -> None:
        """在内存中登记同批未取得结果的工具调用，并更新状态修改时间。

        Args:
            tool_calls (Sequence[ToolCall]): 同一模型步骤中请求的工具调用。
        """
        self.pending_tool_calls = tuple(tool_calls)
        self.updated_at = utc_now()

    def clear_pending_tool_calls(self) -> None:
        """清除内存中的未完成工具调用列表，保存数据库仍由调用方负责。"""
        self.pending_tool_calls = ()
        self.updated_at = utc_now()

    def close_unfinished_tools(
        self, interrupted_content: str, pending_content: str, ordinary_content: str,
        *, interrupted_images: Sequence[str] = (),
    ) -> bool:
        """重建未闭合批次并逐项写入终止结果，不执行工具且保持媒体顺序。

        Args:
            interrupted_content (str): 中断工具的终止结果。
            pending_content (str): 中断后尚未执行工具的取消结果。
            ordinary_content (str): 普通遗留调用的未知结果。
            interrupted_images (Sequence[str]): 已持久桌面回报携带的截图。

        Returns:
            bool: 是否清理了中断或未闭合调用。
        """
        # 1. 收集持久路由，排除已有结果的调用，防止重复收尾。
        interrupt = self.interrupt_data or {}
        had_pending = bool(self.interrupt_data or self.pending_tool_calls)
        closed = {item.tool_call_id for item in self.messages if isinstance(item, ToolMessage)}
        entries: list[tuple[ToolCall, str, Sequence[str]]] = []
        call_id = interrupt.get("tool_call_id")
        if call_id and call_id not in closed:
            entries.append((ToolCall(
                id=call_id, name=interrupt["tool_name"], args=interrupt.get("tool_args") or {},
            ), interrupted_content, interrupted_images))
            closed.add(call_id)
        for call in self.pending_actions:
            if call.id not in closed:
                entries.append((call, pending_content, ()))
                closed.add(call.id)
        for call in self.pending_tool_calls:
            if call.id not in closed:
                entries.append((call, ordinary_content, ()))
                closed.add(call.id)
        # 2. 整批登记后逐项写入完整结果，图片与同一次调用的终止文字一起保存。
        self.set_pending_tool_calls([call for call, _content, _images in entries])
        for call, content, images in entries:
            self.record_tool_result(
                call, (*text_content(content), *(ImageContent(url, media_kind=MediaKind.SCREENSHOT) for url in images)),
            )
        # 3. 在内存中清除中断和待执行信息；调用方随后统一保存工具结果与清理后的状态。
        self.clear_interrupt()
        self.clear_pending_tool_calls()
        return had_pending

    def get_pending_tool_calls(self) -> list[ToolCall]:
        """给调用方提供尚未取得结果的工具调用列表副本。

        Returns:
            list[ToolCall]: 当前 pending_tool_calls 的列表副本。
        """
        return list(self.pending_tool_calls)

    def commit_compaction(self, candidate: Sequence[BaseMessage]) -> None:
        """先检查工具协议和允许的压缩替换，再一次替换内存消息序列；不写数据库。

        Args:
            candidate (Sequence[BaseMessage]): 待检查的候选文本或消息集合。
        """
        replacement = list(candidate)
        self._validate_messages(replacement)
        self._validate_compaction_transition(replacement)
        self._messages = replacement
        self.updated_at = utc_now()

    def replace_message(self, replacement: BaseMessage) -> None:
        """保留身份和协议顺序，原子替换一个消息值。

        Args:
            replacement (BaseMessage): 与原消息编号和时间一致的新消息值。
        """
        index = next(
            (index for index, item in enumerate(self._messages) if item.message_id == replacement.message_id),
            None,
        )
        if index is None:
            raise KeyError(f"消息不存在: {replacement.message_id}")
        candidate = list(self._messages)
        candidate[index] = replacement
        self.commit_compaction(candidate)


    def set_interrupt(
        self,
        interrupt_data: dict[str, Any],
        pending_actions: Sequence[ToolCall | Mapping[str, Any]] | None = None,
    ) -> None:
        """在内存中记录审批或桌面执行的等待信息，以及本批后续尚未执行的调用。

        Args:
            interrupt_data (dict[str, Any]): 当前等待审批或桌面执行的恢复信息。
            pending_actions (Sequence[ToolCall | Mapping[str, Any]] | None): 当前中断之后尚未执行的同批工具调用。
        """
        self.interrupt_data = interrupt_data
        self.pending_actions = tuple(
            item if isinstance(item, ToolCall) else ToolCall.from_dict(item)
            for item in (pending_actions or ())
        )
        self.updated_at = utc_now()

    def get_pending_authorization(self, request_id: str | None) -> dict[str, Any] | None:
        """取得编号匹配且尚无决定的审批快照，不修改状态或判断工具执行权限。

        Scheduler 在决定锁内加载状态后调用，先确认请求有效，再按原顺序保存
        引导消息或记录决定。返回独立快照，审批字段及工具恢复信息仍由状态保存。

        Args:
            request_id (str | None): 命令携带的审批编号，沿用与当前记录相等的检查。

        Returns:
            dict[str, Any] | None: 匹配的未决审批快照，无审批、过期或已决定时为空。
        """
        # 1. 集中当前请求类型、编号和首次决定规则，不接受另一工具调用的旧审批。
        interrupt = self.interrupt_data
        if (not interrupt or interrupt.get("type") != InterruptKind.AUTHORIZATION_REQUEST
                or interrupt.get("request_id") != request_id or interrupt.get("runtime_decision")):
            return None
        return deepcopy(interrupt)

    def record_authorization_decision(self, request_id: str | None, decision: Mapping[str, Any]) -> bool:
        """记录当前审批的首次真人决定，保留工具路由、恢复数据和剩余调用。

        Scheduler 已完成命令校验；引导决定还需先保存新消息到 Inbox。本方法
        只修改当前内存状态，不发放执行授权、不保存 checkpoint 或发布事件。
        返回成功后，由 Scheduler 保存；失败则丢弃该状态并从数据库重新加载。

        Args:
            request_id (str | None): 决定对应的当前审批编号。
            decision (Mapping[str, Any]): 已校验的批准、拒绝或包含新正文的引导决定。

        Returns:
            bool: 请求匹配且首次决定已加入内存状态时为真，过期或重复时为假。

        Raises:
            ValueError: 匹配请求的决定不是批准、拒绝或引导。
        """
        # 1. 再核对未决审批，直接调用本方法也不能覆盖已经记录的决定。
        interrupt = self.get_pending_authorization(request_id)
        if interrupt is None:
            return False
        if decision.get("kind") not in {
            ApprovalDecisionKind.APPROVE, ApprovalDecisionKind.REJECT, ApprovalDecisionKind.STEER,
        }:
            raise ValueError("无效的审批决定")
        # 2. 仅添加决定字段，保留所有原恢复信息；复制决定避免命令载荷后来修改本状态。
        self.set_interrupt(
            {**interrupt, "runtime_decision": deepcopy(dict(decision))}, self.pending_actions,
        )
        return True

    def cancel_pending_interrupt(self) -> bool:
        """沿用新消息取消等待的规则，为尚无决定的当前中断记录系统取消。

        Scheduler 仅在原有等待审批路径、决定锁内调用。本方法沿用已有的中断
        存在且尚无决定的条件，不更改中断类别或桌面领取规则；只更新内存，随后
        由 Scheduler 保存 checkpoint，成功后再通知界面结束等待。

        Returns:
            bool: 已记录系统取消时为真，没有等待或已有决定时为假。
        """
        # 1. 已批准、拒绝、引导或取消的记录保持首次决定，不被后来新消息覆盖。
        interrupt = self.interrupt_data
        if not interrupt or interrupt.get("runtime_decision"):
            return False
        # 2. 只添加取消决定，保留工具恢复数据和剩余调用，供 Agent 随后关闭旧工具协议。
        self.set_interrupt(
            {**interrupt, "runtime_decision": {"kind": AuthorizationResolutionKind.CANCELLED}},
            self.pending_actions,
        )
        return True

    def clear_interrupt(self) -> None:
        """在内存中清除当前等待信息和后续工具调用，不自动执行或保存任何结果。"""
        self.interrupt_data = None
        self.pending_actions = ()
        self.updated_at = utc_now()

    def is_interrupted(self) -> bool:
        """检查内存状态是否仍包含待处理的审批或桌面执行信息。

        Returns:
            bool: interrupt_data 不为空时为 True。
        """
        return self.interrupt_data is not None

    def increment_long_term_memory_turn_counter(self, count: int = 1) -> int:
        """增加已提交真人输入的计数，供记忆插件判断是否达到长期记忆提取间隔。

        Args:
            count (int): 本次已提交的真人输入数量。

        Returns:
            int: 增加后的内存计数；持久保存由调用方完成。
        """
        self.long_term_memory_turn_counter += count
        self.updated_at = utc_now()
        return self.long_term_memory_turn_counter

    def reset_long_term_memory_turn_counter(self) -> None:
        """长期记忆提取完成后清零内存计数，后续 checkpoint 保存该结果。"""
        self.long_term_memory_turn_counter = 0
        self.updated_at = utc_now()

    def set_extra(self, key: str, value: Any) -> None:
        """在内存中保存一个插件或工具的附加状态字段，并更新修改时间。

        Args:
            key (str): 要查找或保存的字段名。
            value (Any): 当前需要校验或转换的值。
        """
        self.extra[key] = value
        self.updated_at = utc_now()

    def get_extra(self, key: str, default: Any = None) -> Any:
        """读取插件或工具的附加状态字段，不存在时返回调用方默认值。

        Args:
            key (str): 要查找或保存的字段名。
            default (Any): 字段不存在时返回的默认值。

        Returns:
            Any: 字段值或 default。
        """
        return self.extra.get(key, default)

    def update_extra(self, data: Mapping[str, Any]) -> None:
        """将插件或工具的附加状态合并到内存字段，并更新修改时间。

        Args:
            data (Mapping[str, Any]): 需要解析或保存的结构化数据。
        """
        self.extra.update(data)
        self.updated_at = utc_now()

    def set_temporary_context(self, key: str, item: TemporaryContextItem) -> None:
        """新增或替换一项临时背景，同一来源重复写入时保持一个条目。

        Args:
            key (str): 来源标识与条目标识，例如 memory.recalled。
            item (TemporaryContextItem): 本轮提供给模型的标题、正文和对应图片。
        """
        self.temporary_context[key] = item

    def remove_temporary_context(self, key: str) -> None:
        """删除某项临时背景。

        Args:
            key (str): 框架或插件此前写入的条目标识。
        """
        self.temporary_context.pop(key, None)

    def clear_temporary_context(self) -> None:
        """新轮开始或原轮结束时清空临时背景，由调用方统一保存状态。"""
        self.temporary_context.clear()

    def to_checkpoint(self) -> dict[str, Any]:
        """检查消息协议后导出完整恢复材料，导出本身不写数据库。

        Returns:
            dict[str, Any]: 包含消息、工具等待、计数、临时背景和附加状态的 checkpoint 字典。

        Raises:
            ValueError: 工具请求/结果顺序或恢复状态不合法。
            TypeError: 消息集合包含非领域消息对象。
        """
        self._validate_messages(self._messages)
        return {
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "session_id": self.session_id,
            "messages": [message.to_storage_format() for message in self._messages],
            "token_baseline": (
                self.token_baseline.model_dump() if self.token_baseline is not None else None
            ),
            "long_term_memory_turn_counter": self.long_term_memory_turn_counter,
            "pending_tool_calls": [call.to_dict() for call in self.pending_tool_calls],
            "interrupt_data": to_json_value(self.interrupt_data),
            "pending_actions": [call.to_dict() for call in self.pending_actions],
            "extra": to_json_value(self.extra),
            "temporary_context": {
                key: item.model_dump() for key, item in self.temporary_context.items()
            },
            "created_at": normalize_utc(self.created_at).isoformat(),
            "updated_at": normalize_utc(self.updated_at).isoformat(),
        }

    @classmethod
    def from_checkpoint(cls, data: Mapping[str, Any]) -> "AgentState":
        """校验 checkpoint 版本并恢复领域消息和未完成工具状态，拒绝不兼容或不完整记录。

        Args:
            data (Mapping[str, Any]): 需要解析或保存的结构化数据。

        Returns:
            AgentState: 已完成消息与恢复状态一致性检查的 AgentState。

        Raises:
            ValueError: 版本、消息结构或工具恢复状态不合法。
        """
        # 1. 先检查版本与列表结构，不猜测旧 checkpoint 的消息契约。
        version = data.get("schema_version")
        if version != CHECKPOINT_SCHEMA_VERSION:
            raise ValueError(
                f"checkpoint schema_version={version!r}，需要先迁移到版本 {CHECKPOINT_SCHEMA_VERSION}"
            )
        raw_messages = data.get("messages")
        if not isinstance(raw_messages, list):
            raise ValueError("checkpoint.messages 必须是数组")
        raw_pending = data.get("pending_tool_calls") or []
        raw_actions = data.get("pending_actions") or []
        if not all(isinstance(items, list) for items in (raw_pending, raw_actions)):
            raise ValueError("checkpoint 工具状态格式无效")
        # 2. 恢复状态字段和完整消息，再确认未闭合调用都有唯一恢复记录。
        state = cls(
            session_id=str(data.get("session_id") or ""),
            token_baseline=data.get("token_baseline"),
            long_term_memory_turn_counter=int(data.get("long_term_memory_turn_counter", 0)),
            pending_tool_calls=tuple(ToolCall.from_dict(item) for item in raw_pending),
            interrupt_data=data.get("interrupt_data"),
            pending_actions=tuple(ToolCall.from_dict(item) for item in raw_actions),
            extra=dict(data.get("extra") or {}),
            temporary_context=data.get("temporary_context") or {},
            created_at=normalize_utc(data.get("created_at") or utc_now()),
            updated_at=normalize_utc(data.get("updated_at") or utc_now()),
        )
        state._messages = [message_from_storage_format(item) for item in raw_messages]
        state._validate_messages(state._messages)
        return state

    @classmethod
    def create_new(cls, session_id: str) -> "AgentState":
        """为尚无历史的会话创建空状态，不建表、不保存 checkpoint。

        Args:
            session_id (str): 当前操作所属的会话编号。

        Returns:
            AgentState: 绑定 session_id 的新 AgentState。
        """
        return cls(session_id=session_id)

    def _append(self, message: BaseMessage) -> None:
        """检查稳定编号未重复后，把消息追加到内存并更新修改时间。

        Args:
            message (BaseMessage): 当前需要读取或追加的消息。

        Raises:
            ValueError: 消息编号已存在。
        """
        if any(item.message_id == message.message_id for item in self._messages):
            raise ValueError(f"消息 ID 重复: {message.message_id}")
        self._messages.append(message)
        self.updated_at = message.created_at


    def _validate_messages(self, messages: Sequence[BaseMessage]) -> None:
        """保存或替换消息前检查工具请求、结果及未完成恢复记录是否逐项一致。

        Args:
            messages (Sequence[BaseMessage]): 按对话顺序排列的消息集合。

        Raises:
            TypeError: 消息集合包含非领域消息。
            ValueError: 编号重复、工具协议被打断，或未闭合调用与恢复记录不一致。
        """
        if not all(isinstance(message, BaseMessage) for message in messages):
            raise TypeError("消息序列只能包含 BaseMessage")
        ids = [message.message_id for message in messages]
        if len(ids) != len(set(ids)):
            raise ValueError("消息序列包含重复 message_id")
        # 1. 沿消息顺序登记请求和结果；一批结果未结束时，真人消息或图片不能插入其间。
        known_calls: dict[str, ToolCall] = {}
        open_calls: dict[str, ToolCall] = {}
        for message in messages:
            if isinstance(message, (SystemMessage, EnvironmentPerceptionMessage)):
                raise ValueError(f"{type(message).__name__} 不得进入 AgentState/checkpoint 消息列表")
            if open_calls and not isinstance(message, ToolMessage):
                raise ValueError(
                    f"工具结果批次被非 ToolMessage 打断: {sorted(open_calls)}"
                )
            if isinstance(message, AssistantMessage):
                for call in message.tool_calls:
                    if call.id in known_calls:
                        raise ValueError(f"重复工具调用 ID: {call.id}")
                    known_calls[call.id] = call
                    open_calls[call.id] = call
            elif isinstance(message, ToolMessage):
                expected = open_calls.get(message.tool_call_id)
                if expected is None:
                    if message.tool_call_id in known_calls:
                        raise ValueError(f"工具调用存在多个或乱序结果: {message.tool_call_id}")
                    raise ValueError(
                        "ToolMessage 找不到对应 assistant tool call: "
                        f"{message.tool_call_id}"
                    )
                if message.tool_name != expected.name:
                    raise ValueError(f"工具调用名称不匹配: {message.tool_call_id}")
                del open_calls[message.tool_call_id]

        # 2. 未闭合请求必须逐项出现在普通待处理列表，或中断工具及其后续调用中。
        unresolved = set(open_calls)
        recoverable: dict[str, str] = {}

        def add_recovery(call_id: str, tool_name: str, source: str) -> None:
            """校验恢复材料时登记每个未完成调用，拒绝同一调用同时出现在两个恢复位置。

            Args:
                call_id (str): 恢复材料中的工具调用编号。
                tool_name (str): 注册表中使用的工具名称。
                source (str): 当前记录来自 pending_tool_calls、interrupt 或 pending_actions。

            Raises:
                ValueError: 该调用编号已在另一处恢复材料中登记。
            """
            if call_id in recoverable:
                raise ValueError(f"工具恢复状态重复引用调用: {call_id} ({source})")
            recoverable[call_id] = tool_name

        if self.interrupt_data is None:
            if self.pending_actions:
                raise ValueError("pending_actions 必须依附 interrupt 恢复状态")
            for call in self.pending_tool_calls:
                add_recovery(call.id, call.name, "pending_tool_calls")
        else:
            if self.pending_tool_calls:
                raise ValueError("中断恢复状态不得同时保留 pending_tool_calls")
            interrupted_call_id = self.interrupt_data.get("tool_call_id")
            if not isinstance(interrupted_call_id, str) or not interrupted_call_id:
                raise ValueError("中断恢复状态缺少 tool_call_id")
            interrupted_tool_name = self.interrupt_data.get("tool_name")
            if not isinstance(interrupted_tool_name, str) or not interrupted_tool_name:
                raise ValueError("中断恢复状态缺少 tool_name")
            add_recovery(interrupted_call_id, interrupted_tool_name, "interrupt")
            for call in self.pending_actions:
                add_recovery(call.id, call.name, "pending_actions")

        # 3. 两边编号和工具名称必须完全对应，既不允许漏记，也不允许恢复已完成调用。
        recoverable_ids = set(recoverable)
        if recoverable_ids - unresolved:
            raise ValueError(
                "工具恢复状态指向已闭合或不存在的调用: "
                f"{sorted(recoverable_ids - unresolved)}"
            )
        if unresolved - recoverable_ids:
            raise ValueError(f"工具协议未闭合: {sorted(unresolved - recoverable_ids)}")
        for call_id, tool_name in recoverable.items():
            if open_calls[call_id].name != tool_name:
                raise ValueError(f"工具恢复状态名称不匹配: {call_id}")

    def _validate_compaction_transition(
        self,
        candidate: Sequence[BaseMessage],
    ) -> None:
        """只允许删除、同身份替换，以及插入上下文摘要。

        Args:
            candidate (Sequence[BaseMessage]): 待检查的候选文本或消息集合。
        """
        previous_by_id = {message.message_id: message for message in self._messages}
        previous_order = {
            message.message_id: index for index, message in enumerate(self._messages)
        }
        retained_order: list[int] = []
        for message in candidate:
            previous = previous_by_id.get(message.message_id)
            if previous is None:
                if not isinstance(message, ContextSummaryMessage):
                    raise ValueError("compaction 只能新增 ContextSummaryMessage")
                continue
            if type(message) is not type(previous):
                raise ValueError(f"compaction 不得改变消息类型: {message.message_id}")
            if message.created_at != previous.created_at:
                raise ValueError(f"compaction 不得改变消息时间: {message.message_id}")
            retained_order.append(previous_order[message.message_id])
        if retained_order != sorted(retained_order):
            raise ValueError("compaction 不得改变保留消息的相对顺序")
