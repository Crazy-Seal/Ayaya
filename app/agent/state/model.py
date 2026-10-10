"""通过语义消息 Interface 管理可恢复的 Agent 状态。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from app.agent.state.message_names import (
    MCP_TOOL_IMAGE_MESSAGE_NAME,
    SCREENSHOT_MESSAGE_NAME,
    TOOL_IMAGE_MESSAGE_NAME,
)
from app.agent.state.messages import (
    AssistantMessage,
    AvailableMedia,
    BaseMessage,
    ContextSummaryMessage,
    HumanMessage,
    RuntimeInputMessage,
    SystemMessage,
    ToolCall,
    ToolMediaMessage,
    ToolMessage,
    message_from_storage_format,
    messages_to_openai_format,
    to_json_value,
)
from app.agent.state.time import normalize_utc, utc_now
from app.schemas.runtime_types import ApprovalDecisionKind, AuthorizationResolutionKind, InterruptKind


CHECKPOINT_SCHEMA_VERSION = 2


class TokenBaseline(BaseModel):
    """一次成功主模型请求留下的真实输入 token 锚点。"""

    accounting_key: str
    estimator_version: int
    static_fingerprint: str
    rough_prompt_tokens: int = Field(gt=0)
    actual_prompt_tokens: int = Field(gt=0)


@dataclass(frozen=True, slots=True)
class _DeferredToolMedia:
    tool_call_id: str
    tool_name: str
    associated_message_id: str
    image_message_name: str
    image_urls: tuple[str, ...]

    def to_storage_format(self) -> dict[str, Any]:
        return {
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "associated_message_id": self.associated_message_id,
            "image_message_name": self.image_message_name,
            "image_urls": list(self.image_urls),
        }

    @classmethod
    def from_storage_format(cls, data: Mapping[str, Any]) -> "_DeferredToolMedia":
        urls = data.get("image_urls") or []
        if not isinstance(urls, list) or not all(isinstance(url, str) for url in urls):
            raise ValueError("_deferred_tool_media.image_urls 必须是字符串数组")
        return cls(
            tool_call_id=str(data.get("tool_call_id") or ""),
            tool_name=str(data.get("tool_name") or ""),
            associated_message_id=str(data.get("associated_message_id") or ""),
            image_message_name=str(data.get("image_message_name") or TOOL_IMAGE_MESSAGE_NAME),
            image_urls=tuple(urls),
        )


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
        created_at (datetime): 原消息 UTC 创建时间。
        updated_at (datetime): 状态最近修改的 UTC 时间。
        _messages (list[BaseMessage]): 需要转换或交付的领域消息集合。
        _deferred_tool_media (list[_DeferredToolMedia]): 等整批工具调用闭合后再追加的媒体消息。
    """

    session_id: str
    token_baseline: TokenBaseline | None = None
    long_term_memory_turn_counter: int = 0
    pending_tool_calls: tuple[ToolCall, ...] = ()
    interrupt_data: dict[str, Any] | None = None
    pending_actions: tuple[ToolCall, ...] = ()
    extra: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    _messages: list[BaseMessage] = PrivateAttr(default_factory=list)
    _deferred_tool_media: list[_DeferredToolMedia] = PrivateAttr(default_factory=list)

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    @property
    def messages(self) -> tuple[BaseMessage, ...]:
        """返回当前可恢复协议的不可变快照。"""
        return tuple(self._messages)

    @property
    def deferred_tool_media_count(self) -> int:
        return len(self._deferred_tool_media)

    def append_input(self, message: HumanMessage | RuntimeInputMessage) -> None:
        if not isinstance(message, (HumanMessage, RuntimeInputMessage)):
            raise TypeError("Agent 输入必须是 HumanMessage 或 RuntimeInputMessage")
        self._append(message)

    def add_user_message(
        self,
        text: str,
        *,
        image_urls: Sequence[str] = (),
        image_filenames: Sequence[str] = (),
        origin_channel: str = "desktop",
    ) -> HumanMessage:
        """构造带来源的真人消息并追加到经过协议校验的输入序列。

        Args:
            text (str): 需要保存或追加的文本。
            image_urls (Sequence[str]): 真人附件的模型图片地址。
            image_filenames (Sequence[str]): 已外置保存的附件路径。
            origin_channel (str): 输入或本轮回复所属的渠道。

        Returns:
            HumanMessage: 当前查询或执行结果。
        """
        message = HumanMessage(
            text=text,
            image_urls=tuple(image_urls),
            image_filenames=tuple(image_filenames),
            origin_channel=origin_channel,
        )
        self.append_input(message)
        return message

    def record_assistant_step(
        self,
        content: str,
        tool_calls: Sequence[ToolCall] = (),
        *,
        origin_channel: str = "desktop",
    ) -> AssistantMessage:
        """记录带来源的 Assistant 步骤，上一批工具未闭合时禁止开始下一批。

        Args:
            content (str): Assistant 正文。
            tool_calls (Sequence[ToolCall]): 本步模型请求的工具调用。
            origin_channel (str): 输入或本轮回复所属的渠道。

        Returns:
            AssistantMessage: 当前查询或执行结果。

        Raises:
            ValueError: 上一批工具调用尚未闭合，不能记录新的 assistant step。
        """
        calls = tuple(tool_calls)
        if self.pending_tool_calls:
            raise ValueError("上一批工具调用尚未闭合，不能记录新的 assistant step")
        message = AssistantMessage(text=content, tool_calls=calls, origin_channel=origin_channel)
        self._append(message)
        self.pending_tool_calls = calls
        return message

    def add_assistant_message(self, content: str, *, origin_channel: str = "desktop") -> AssistantMessage:
        """通过统一 Assistant 步骤入口记录纯文本回复及来源。

        Args:
            content (str): Assistant 正文。
            origin_channel (str): 输入或本轮回复所属的渠道。

        Returns:
            AssistantMessage: 当前查询或执行结果。
        """
        return self.record_assistant_step(content, origin_channel=origin_channel)

    def record_tool_result(
        self,
        tool_call: ToolCall,
        content: str,
        *,
        compressible: bool = False,
        image_urls: Sequence[str] = (),
        image_message_name: str = TOOL_IMAGE_MESSAGE_NAME,
    ) -> ToolMessage:
        pending_call = next(
            (candidate for candidate in self.pending_tool_calls if candidate.id == tool_call.id),
            None,
        )
        if pending_call is None:
            raise ValueError(f"工具调用未处于待闭合批次: {tool_call.id}")
        if pending_call.name != tool_call.name:
            raise ValueError(f"工具调用名称不匹配: {tool_call.id}")
        message = ToolMessage(
            content=content,
            tool_name=pending_call.name,
            tool_call_id=pending_call.id,
            compressible=compressible,
        )
        self._append(message)
        self.pending_tool_calls = tuple(
            candidate
            for candidate in self.pending_tool_calls
            if candidate.id != pending_call.id
        )
        urls = tuple(image_urls)
        if urls:
            self._deferred_tool_media.append(_DeferredToolMedia(
                tool_call_id=pending_call.id,
                tool_name=pending_call.name,
                associated_message_id=message.message_id,
                image_message_name=image_message_name,
                image_urls=urls,
            ))
        if not self.pending_tool_calls:
            self._flush_deferred_tool_media()
        return message

    def add_tool_message(
        self,
        content: str,
        tool_name: str,
        tool_call_id: str,
        *,
        compressible: bool = False,
    ) -> ToolMessage:
        """仅剩持久化路由时记录合成或取消结果。"""
        call = next(
            (
                candidate for candidate in self.pending_tool_calls
                if candidate.id == tool_call_id and candidate.name == tool_name
            ),
            None,
        )
        if call is None:
            raise ValueError(f"工具调用未处于待闭合批次: {tool_call_id}")
        return self.record_tool_result(call, content, compressible=compressible)

    def set_pending_tool_calls(self, tool_calls: Sequence[ToolCall]) -> None:
        self.pending_tool_calls = tuple(tool_calls)
        self.updated_at = utc_now()

    def clear_pending_tool_calls(self) -> None:
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
        # 2. 整批登记后再写结果，延迟图片必须等所有 ToolMessage 闭合才刷新。
        self.set_pending_tool_calls([call for call, _content, _images in entries])
        for call, content, images in entries:
            self.record_tool_result(
                call, content, image_urls=images, image_message_name=SCREENSHOT_MESSAGE_NAME,
            )
        # 3. 在内存中清除中断和待执行信息；调用方随后统一保存工具结果与清理后的状态。
        self.clear_interrupt()
        self.clear_pending_tool_calls()
        return had_pending

    def get_pending_tool_calls(self) -> list[ToolCall]:
        return list(self.pending_tool_calls)

    def commit_compaction(self, candidate: Sequence[BaseMessage]) -> None:
        """通过协议校验后原子替换消息序列。"""
        replacement = list(candidate)
        self._validate_messages(replacement)
        self._validate_compaction_transition(replacement)
        self._messages = replacement
        self.updated_at = utc_now()

    def replace_message(self, replacement: BaseMessage) -> None:
        """保留身份和协议顺序，原子替换一个消息值。"""
        index = next(
            (index for index, item in enumerate(self._messages) if item.message_id == replacement.message_id),
            None,
        )
        if index is None:
            raise KeyError(f"消息不存在: {replacement.message_id}")
        candidate = list(self._messages)
        candidate[index] = replacement
        self.commit_compaction(candidate)

    def get_openai_messages(self) -> list[dict[str, Any]]:
        return messages_to_openai_format(self.messages)

    def set_interrupt(
        self,
        interrupt_data: dict[str, Any],
        pending_actions: Sequence[ToolCall | Mapping[str, Any]] | None = None,
    ) -> None:
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
        self.interrupt_data = None
        self.pending_actions = ()
        self.updated_at = utc_now()

    def is_interrupted(self) -> bool:
        return self.interrupt_data is not None

    def increment_long_term_memory_turn_counter(self, count: int = 1) -> int:
        self.long_term_memory_turn_counter += count
        self.updated_at = utc_now()
        return self.long_term_memory_turn_counter

    def reset_long_term_memory_turn_counter(self) -> None:
        self.long_term_memory_turn_counter = 0
        self.updated_at = utc_now()

    def set_extra(self, key: str, value: Any) -> None:
        self.extra[key] = value
        self.updated_at = utc_now()

    def get_extra(self, key: str, default: Any = None) -> Any:
        return self.extra.get(key, default)

    def update_extra(self, data: Mapping[str, Any]) -> None:
        self.extra.update(data)
        self.updated_at = utc_now()

    def to_checkpoint(self) -> dict[str, Any]:
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
            "_deferred_tool_media": [
                item.to_storage_format() for item in self._deferred_tool_media
            ],
            "interrupt_data": to_json_value(self.interrupt_data),
            "pending_actions": [call.to_dict() for call in self.pending_actions],
            "extra": to_json_value(self.extra),
            "created_at": normalize_utc(self.created_at).isoformat(),
            "updated_at": normalize_utc(self.updated_at).isoformat(),
        }

    @classmethod
    def from_checkpoint(cls, data: Mapping[str, Any]) -> "AgentState":
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
        raw_deferred = data.get("_deferred_tool_media") or []
        if not all(isinstance(items, list) for items in (raw_pending, raw_actions, raw_deferred)):
            raise ValueError("checkpoint 工具状态格式无效")
        state = cls(
            session_id=str(data.get("session_id") or ""),
            token_baseline=data.get("token_baseline"),
            long_term_memory_turn_counter=int(data.get("long_term_memory_turn_counter", 0)),
            pending_tool_calls=tuple(ToolCall.from_dict(item) for item in raw_pending),
            interrupt_data=data.get("interrupt_data"),
            pending_actions=tuple(ToolCall.from_dict(item) for item in raw_actions),
            extra=dict(data.get("extra") or {}),
            created_at=normalize_utc(data.get("created_at") or utc_now()),
            updated_at=normalize_utc(data.get("updated_at") or utc_now()),
        )
        state._messages = [message_from_storage_format(item) for item in raw_messages]
        state._deferred_tool_media = [
            _DeferredToolMedia.from_storage_format(item) for item in raw_deferred
        ]
        state._validate_messages(state._messages)
        return state

    @classmethod
    def create_new(cls, session_id: str) -> "AgentState":
        return cls(session_id=session_id)

    def _append(self, message: BaseMessage) -> None:
        if any(item.message_id == message.message_id for item in self._messages):
            raise ValueError(f"消息 ID 重复: {message.message_id}")
        self._messages.append(message)
        self.updated_at = message.created_at

    def _flush_deferred_tool_media(self) -> None:
        deferred = list(self._deferred_tool_media)
        self._deferred_tool_media.clear()
        for item in deferred:
            media_kind = {
                SCREENSHOT_MESSAGE_NAME: "screenshot",
                MCP_TOOL_IMAGE_MESSAGE_NAME: "mcp",
                TOOL_IMAGE_MESSAGE_NAME: "tool",
            }.get(item.image_message_name)
            if media_kind is None:
                raise ValueError(f"未知工具图片类型: {item.image_message_name}")
            self._append(ToolMediaMessage(
                tool_name=item.tool_name,
                media_kind=media_kind,
                text=f"[系统消息]工具 {item.tool_name} 返回图片: ",
                payload=AvailableMedia(urls=item.image_urls),
                associated_message_id=item.associated_message_id,
            ))

    def _validate_messages(self, messages: Sequence[BaseMessage]) -> None:
        if not all(isinstance(message, BaseMessage) for message in messages):
            raise TypeError("消息序列只能包含 BaseMessage")
        ids = [message.message_id for message in messages]
        if len(ids) != len(set(ids)):
            raise ValueError("消息序列包含重复 message_id")
        known_calls: dict[str, ToolCall] = {}
        open_calls: dict[str, ToolCall] = {}
        for message in messages:
            if isinstance(message, SystemMessage):
                raise ValueError("SystemMessage 不得进入 AgentState/checkpoint")
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

        unresolved = set(open_calls)
        recoverable: dict[str, str] = {}

        def add_recovery(call_id: str, tool_name: str, source: str) -> None:
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
        """只允许删除、同身份替换，以及插入上下文摘要。"""
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
