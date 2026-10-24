"""协调会话执行并暂存输出；渠道连接由 EventHub 主动读取。"""

from __future__ import annotations

from contextlib import aclosing

import asyncio
from dataclasses import asdict, dataclass, field, replace
import logging
from typing import AsyncIterator, Callable
from uuid import uuid4

from app.agent.agent import Agent
from app.agent.core.events import AgentEvent, EventType
from app.agent.runtime.session_inbox import InboxEnvelope
from app.agent.runtime.turn_control import TurnControl
from app.agent.state.manager import SessionStateManager
from app.agent.state.messages import ImageContent
from app.crud.task_record_dao import TaskRecord
from app.schemas.runtime_types import (
    ApprovalDecisionKind, AuthorizationResolutionKind, CommandKind, DESKTOP_CHANNEL,
    InboxDeliveryMode, InboxKind, InteractionEventKind, SessionPhase,
    TaskStatus, TurnInterruptReason, TurnPhase, RuntimePhase, InboxStatus,
)
from app.interaction.interaction_contract import ChannelCommand, CommandReceipt, InteractionEvent
from app.orchestration.task_supervisor import TaskSupervisor
from app.services.channel_subscription_service import ChannelSubscriptionService


logger = logging.getLogger(__name__)
INPUT_DELIVERY_MAX_ATTEMPTS = 4
AGENT_TO_INTERACTION_EVENT = {
    EventType.TEXT_CHUNK: InteractionEventKind.TEXT_CHUNK,
    EventType.TOOL_CALL: InteractionEventKind.TOOL_CALL,
    EventType.INTERRUPT: InteractionEventKind.INTERRUPT,
    EventType.ERROR: InteractionEventKind.ERROR,
    EventType.DONE: InteractionEventKind.RUN_COMPLETED,
}


@dataclass
class _CurrentTurn:
    """保存一轮回答的身份和当前执行材料，不保存第二份会话阶段。

    Attributes:
        run_id (str): 开始执行时生成，审批恢复时沿用。
        origin (str): 本轮输入渠道。
        control (TurnControl | None): 本次 Agent 的控制对象；审批等待时可为空。
        approval (dict | None): 给界面显示的审批内容，有效性仍以 checkpoint 为准。
        started (bool): 本次执行是否已经报告开始，输入重试不重复报告。
    """

    run_id: str
    origin: str
    control: TurnControl | None = None
    approval: dict | None = None
    started: bool = False


@dataclass
class _SessionState:
    """集中保存会话的内存协调数据，本记录本身不执行任何业务。

    Attributes:
        worker (asyncio.Task | None): 长期检查本会话工作的任务。
        wakeup (asyncio.Event): 新输入、审批决定或任务通知到达的提示。
        lock (asyncio.Lock): 协调命令、读取和轮次状态变化。
        activated (bool): 是否允许自动执行；真人输入受理后恢复，用户停止受理后撤销。
        sequence (int): 本次后端运行中该会话已记录的事件编号。
        current (_CurrentTurn | None): 当前回答；为空表示空闲。
        resume_request_id (str | None): 待处理的审批编号，不复制已保存的决定。
        shutdown_partial (tuple | None): 关闭前捕获的正文、记录标记、来源及消息身份。
    """

    worker: asyncio.Task | None = None
    wakeup: asyncio.Event = field(default_factory=asyncio.Event)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    activated: bool = False
    sequence: int = 0
    current: _CurrentTurn | None = None
    resume_request_id: str | None = None
    shutdown_partial: tuple[str, bool, str, str | None] | None = None

    @property
    def phase(self) -> SessionPhase:
        """从当前轮次生成阶段，不独立写入另一个展示状态。

        Returns:
            SessionPhase: 空闲、执行中或等待审批。
        """
        if self.current is None:
            return SessionPhase.IDLE
        return SessionPhase.PENDING_INTERRUPT if self.current.approval is not None else SessionPhase.RUNNING


class AgentRuntimeScheduler:
    """受理命令、执行会话并保存临时输出，不持有渠道分发对象。

    Attributes:
        subscriptions (ChannelSubscriptionService): 命令入口与 Hub 共用的配置。
        task_supervisor (TaskSupervisor | None): 查询、停止后台任务并读取记录的共享对象，不拥有其生命周期。
        phase (RuntimePhase): 后端执行入口的生命周期。
        _sessions (dict[str, _SessionState]): 内存会话记录，不表示已经持久保存。
        _output (asyncio.Queue): 由唯一 Hub 读取的临时事件队列。
        _epoch (str): 本次 Scheduler 实例的事件编号标识。
        _lifecycle_lock (asyncio.Lock): 启动、命令受理与开始退出之间的互斥。
        _task_reader (asyncio.Task | None): 主动读取 Supervisor 已保存记录的唯一任务。
    """

    def __init__(
        self, *, agent_factory: Callable[[str, SessionStateManager], Agent],
        subscriptions: ChannelSubscriptionService | None = None,
        session_state_manager_factory: Callable[[str], SessionStateManager] = SessionStateManager,
        task_supervisor: TaskSupervisor | None = None,
    ) -> None:
        """保存执行依赖，尚不创建 worker 或 Agent。

        Args:
            agent_factory (Callable[[str, SessionStateManager], Agent]): 按会话创建本次 Agent。
            subscriptions (ChannelSubscriptionService | None): 应用注入的共享配置，测试可省略。
            session_state_manager_factory (Callable[[str], SessionStateManager]): 当前会话的状态业务对象工厂。
            task_supervisor (TaskSupervisor | None): 共享任务查询与停止入口。
        """
        self.subscriptions = subscriptions or ChannelSubscriptionService()
        self.task_supervisor = task_supervisor
        self._agent_factory = agent_factory
        self._manager_factory = session_state_manager_factory
        self._sessions: dict[str, _SessionState] = {}
        self._output: asyncio.Queue[InteractionEvent | None] = asyncio.Queue()
        self._output_closed = False
        self._epoch = uuid4().hex
        self._lifecycle_lock = asyncio.Lock()
        self._task_reader: asyncio.Task[None] | None = None
        self.phase = RuntimePhase.INITIALIZING

    def _session(self, session_id: str) -> _SessionState:
        """取得或创建内存记录；读取记录不会获得执行资格。

        Args:
            session_id (str): 会话编号。

        Returns:
            _SessionState: 此会话唯一的内存记录。
        """
        if session_id not in self._sessions:
            self._sessions[session_id] = _SessionState()
        return self._sessions[session_id]

    def _emit(self, event: InteractionEvent) -> None:
        """同步编号并暂存事件，状态修改调用方必须已持会话锁。

        本方法不等待、不发送网络消息，状态变化到入队之间不会切换协程。

        Args:
            event (InteractionEvent): 已完成前置保存的业务通知。
        """
        if self._output_closed:
            return
        session = self._session(event.session_id)
        session.sequence += 1
        self._output.put_nowait(replace(event, stream_epoch=self._epoch, event_sequence=session.sequence))

    async def next_event(self) -> InteractionEvent | None:
        """供 Hub 独占等待并读取输出，读取后不保留历史。

        Returns:
            InteractionEvent | None: 下一条事件；输出关闭且无剩余数据时为空。
        """
        if self._output_closed and self._output.empty():
            return None
        return await self._output.get()

    async def start(self) -> None:
        """清理旧输入和未结束轮次，再开放命令并启动唯一任务记录消费者。

        Raises:
            RuntimeError: 已关闭的实例不能重新启动。
        """
        async with self._lifecycle_lock:
            if self.phase == RuntimePhase.RUNNING:
                return
            if self.phase != RuntimePhase.INITIALIZING:
                raise RuntimeError("已关闭的运行时不能重新启动")
            await self._reset_inbox()
            await self._finalize_abandoned_sessions()
            self.phase = RuntimePhase.RUNNING
            if self.task_supervisor is not None:
                self._task_reader = asyncio.create_task(self._receive_task_records(self.task_supervisor))

    async def _reset_inbox(self) -> None:
        """作废旧未交付真人意图，保留任务通知和已交付消息。"""
        manager = self._manager_factory("__runtime_init__")
        try:
            await manager.reset_after_restart()
        finally:
            await manager.close()

    async def _finalize_abandoned_sessions(self) -> None:
        """只保存未结束轮次的终止事实，不创建 Agent 或重跑工具。"""
        manager = self._manager_factory("__runtime_init__")
        try:
            sessions = await manager.unfinished_sessions()
        finally:
            await manager.close()
        for session_id in sessions:
            manager = self._manager_factory(session_id)
            partial = self._session(session_id).shutdown_partial or ("", False, DESKTOP_CHANNEL, None)
            try:
                await manager.finalize_abandoned_turn(
                    partial=partial[0], partial_persisted=partial[1],
                    origin_channel=partial[2], partial_message_id=partial[3],
                )
            finally:
                await manager.close()

    async def snapshot(self, session_id: str) -> InteractionEvent:
        """复制状态及对应编号，再查询持久输入和任务，不启动执行。

        Args:
            session_id (str): 共享会话编号。

        Returns:
            InteractionEvent: 未按渠道筛选的状态，数据库查询不属于同一时点事务。
        """
        session = self._session(session_id)
        async with session.lock:
            # 1. 状态和事件编号一起复制，数据库等待期间可继续产生更新事件。
            current = session.current
            payload = {
                "phase": session.phase, "run_id": current.run_id if current else None,
                "origin_channel": current.origin if current else None,
                "interrupt": current.approval if current else None,
            }
            sequence = session.sequence
        # 2. 查询独立的数据库事实，Hub 会缓冲查询期间到达的后续事件。
        manager = self._manager_factory(session_id)
        try:
            pending = await manager.pending_humans()
        finally:
            await manager.close()
        payload["pending_inputs"] = [
            {"input_sequence": item.id, "origin_channel": item.origin_channel, "delivery_mode": item.delivery_mode}
            for item in pending
        ]
        payload["queued_inputs"] = [
            {"input_sequence": item.id, "message": item.payload["message"],
             "images": item.payload.get("images") or [], "origin_channel": item.origin_channel}
            for item in pending if item.delivery_mode == InboxDeliveryMode.QUEUED
        ]
        payload["tasks"] = (
            [asdict(item) for item in await self.task_supervisor.list(session_id)]
            if self.task_supervisor is not None else []
        )
        return InteractionEvent(
            session_id, InteractionEventKind.SESSION_SNAPSHOT, payload,
            stream_epoch=self._epoch, event_sequence=sequence,
        )

    async def submit(self, command: ChannelCommand) -> CommandReceipt:
        """检查生命周期、渠道，再在会话锁内受理命令。

        Args:
            command (ChannelCommand): 渠道已经归一化的用户动作。

        Returns:
            CommandReceipt: 受理结果，不预留下一轮编号。
        """
        async with self._lifecycle_lock:
            if self.phase != RuntimePhase.RUNNING:
                return CommandReceipt(False, "运行时尚未就绪或正在关闭")
            try:
                self.subscriptions.validate_source(command.origin_channel)
                if not await self.subscriptions.is_enabled(command.session_id, command.origin_channel):
                    raise ValueError("该会话未订阅此渠道")
            except ValueError as exc:
                return CommandReceipt(False, str(exc))
            # 1. 停止进程只校验任务归属，不需要占用会话执行状态的锁。
            if command.kind == CommandKind.STOP_BACKGROUND_TASK:
                return await self._stop_background_task(command)
            handlers = {
                CommandKind.CANCEL_QUEUED: self._cancel_queued_input,
                CommandKind.STOP_TURN: self._stop_current_turn,
                CommandKind.RESOLVE_AUTHORIZATION: self._resolve_authorization,
                CommandKind.HUMAN_TURN: self._submit_human_input,
                CommandKind.HUMAN_STEER: self._submit_human_input,
                CommandKind.QUEUE_TURN: self._submit_human_input,
            }
            handler = handlers.get(command.kind)
            if handler is None:
                return CommandReceipt(False, "当前命令尚未接入运行时")
            # 2. 固定生命周期锁到会话锁的顺序；worker 不取得生命周期锁。
            async with self._session(command.session_id).lock:
                return await handler(command)

    async def _stop_background_task(self, command: ChannelCommand) -> CommandReceipt:
        """停止本会话所属的独立任务，不停止聊天。

        Args:
            command (ChannelCommand): 已验证来源的停止任务命令。

        Returns:
            CommandReceipt: Supervisor 是否受理停止。
        """
        if self.task_supervisor is None or not command.task_id:
            return CommandReceipt(False, "后台任务不存在")
        record = await self.task_supervisor.get(command.task_id)
        if record is None or record.session_id != command.session_id:
            return CommandReceipt(False, "后台任务不存在")
        return CommandReceipt(await self.task_supervisor.stop(command.task_id))

    async def _cancel_queued_input(self, command: ChannelCommand) -> CommandReceipt:
        """删除本渠道尚未交付的排队消息，提交成功才产生移除事件。

        Args:
            command (ChannelCommand): 携带 input_sequence 的撤销命令。

        Returns:
            CommandReceipt: 是否删除；已交付或不存在的输入不能撤销。
        """
        sequence = command.payload.get("input_sequence")
        if not isinstance(sequence, int):
            return CommandReceipt(False, "缺少排队消息编号")
        manager = self._manager_factory(command.session_id)
        try:
            cancelled = await manager.cancel_queued(sequence, command.origin_channel)
        finally:
            await manager.close()
        if cancelled:
            self._emit(InteractionEvent(command.session_id, InteractionEventKind.QUEUED_CANCELLED,
                {"input_sequence": sequence}, origin_channel=command.origin_channel))
        return CommandReceipt(cancelled, None if cancelled else "排队消息已交付或不存在")

    async def _stop_current_turn(self, command: ChannelCommand) -> CommandReceipt:
        """对已开始的匹配轮次请求停止，不取消尚未执行的输入。

        Args:
            command (ChannelCommand): 可选携带目标回答编号。

        Returns:
            CommandReceipt: 已受理，或无匹配轮次、正在最终提交。
        """
        session = self._session(command.session_id)
        current = session.current
        control = current.control if current else None
        if (control is None or session.phase != SessionPhase.RUNNING
                or (command.target_run_id and command.target_run_id != control.run_id)):
            return CommandReceipt(False, "没有匹配的活动轮次")
        if not control.request(TurnInterruptReason.STOP):
            return CommandReceipt(False, "当前轮次正在提交，已无法停止")
        # 1. 用户停止成功后暂停后续自动执行，通知继续保存，待下次真人输入恢复。
        session.activated = False
        return CommandReceipt(True, run_id=control.run_id)

    async def _submit_human_input(self, command: ChannelCommand) -> CommandReceipt:
        """先保存真人输入，再打断回答或取消审批，最后唤醒 worker。

        Args:
            command (ChannelCommand): 普通消息、引导或排队命令。

        Returns:
            CommandReceipt: 已受理及当前轮次编号，不代表本条输入已经执行。
        """
        if command.kind == CommandKind.HUMAN_STEER and not command.target_run_id:
            return CommandReceipt(False, "引导缺少目标轮次")
        message, images = command.payload.get("message"), command.payload.get("images")
        if not isinstance(message, str) or (not message.strip() and not images):
            return CommandReceipt(False, "消息不能为空")
        session = self._session(command.session_id)
        current = session.current
        phase = session.phase
        control = current.control if current else None
        committing = control is not None and control.phase == TurnPhase.COMMITTING
        if command.kind == CommandKind.QUEUE_TURN:
            if current is None:
                return CommandReceipt(False, "当前没有可排队的活动轮次")
            if committing:
                return CommandReceipt(False, "当前轮次正在提交，请作为新消息发送")
        queued = command.kind == CommandKind.QUEUE_TURN
        if command.kind == CommandKind.HUMAN_STEER:
            # 1. 等待当前轮次审批也属于有效引导；仅指向另一活动轮次时沿用排队策略。
            target_is_active = current is not None and command.target_run_id == current.run_id and not committing
            if not target_is_active and not committing and current is not None:
                queued = True
        manager = self._manager_factory(command.session_id)
        try:
            envelope = await manager.submit_human(command.origin_channel, message, images=images, queued=queued)
        finally:
            await manager.close()
        # 2. 保存失败不会打断旧回答；保存成功后获得本次启动的执行资格。
        session.activated = True
        if not queued and phase == SessionPhase.RUNNING and control is not None:
            if command.kind != CommandKind.HUMAN_STEER or control.run_id == command.target_run_id:
                control.request(TurnInterruptReason.STEER)
        if queued:
            self._emit(InteractionEvent(command.session_id, InteractionEventKind.QUEUED_MESSAGE,
                {"input_sequence": envelope.id, "message": message, "images": images or []},
                origin_channel=command.origin_channel))
        if not queued and phase == SessionPhase.PENDING_INTERRUPT:
            await self._cancel_pending_interrupt(command.session_id, command.origin_channel)
        self._ensure_running(command.session_id)
        current = session.current
        return CommandReceipt(True, run_id=current.run_id if current else None)

    async def _cancel_pending_interrupt(self, session_id: str, actor: str) -> None:
        """已保存的新输入取消未决审批；调用方已经持会话锁。

        Args:
            session_id (str): 新输入所属会话。
            actor (str): 取消审批的来源渠道。
        """
        session = self._session(session_id)
        manager = self._manager_factory(session_id)
        try:
            cancelled = await manager.cancel_pending_interrupt()
            if cancelled is None:
                return
            request_id, origin = cancelled
            run_id = session.current.run_id if session.current else None
            # 1. 持久决定保存成功才结束旧轮次，两个事件之间不等待。
            session.current = None
            self._emit(InteractionEvent(session_id, InteractionEventKind.AUTHORIZATION_RESOLVED,
                {"outcome": AuthorizationResolutionKind.CANCELLED, "actor_channel": actor},
                origin_channel=origin, request_id=request_id))
            self._emit(InteractionEvent(session_id, InteractionEventKind.RUN_COMPLETED,
                {"value": {"interrupted": True}}, origin_channel=origin, run_id=run_id))
        finally:
            await manager.close()

    async def _resolve_authorization(self, command: ChannelCommand) -> CommandReceipt:
        """锁内重读并保存首次决定；只通知待恢复编号，不复制决定。

        Args:
            command (ChannelCommand): 批准、拒绝或审批框新指令。

        Returns:
            CommandReceipt: 受理结果或请求已过期。
        """
        decision = command.payload
        if decision.get("kind") not in {ApprovalDecisionKind.APPROVE, ApprovalDecisionKind.REJECT, ApprovalDecisionKind.STEER}:
            return CommandReceipt(False, "无效的审批决定")
        steer = decision["kind"] == ApprovalDecisionKind.STEER
        if steer and not str(decision.get("message") or "").strip():
            return CommandReceipt(False, "引导消息不能为空")
        session = self._session(command.session_id)
        manager = self._manager_factory(command.session_id)
        try:
            origin = await manager.record_authorization_decision(
                command.request_id, decision, command.origin_channel,
            )
            if origin is None:
                return CommandReceipt(False, "审批请求已处理或过期")
            # 2. 决定只存在 checkpoint；内存只保留待处理请求编号。
            if session.current:
                session.current.approval = None
            self._emit(InteractionEvent(command.session_id, InteractionEventKind.AUTHORIZATION_RESOLVED,
                {"outcome": AuthorizationResolutionKind(decision["kind"]), "actor_channel": command.origin_channel},
                origin_channel=origin, request_id=command.request_id))
            if steer:
                run_id = session.current.run_id if session.current else None
                session.current = None
                session.activated = True
                self._emit(InteractionEvent(command.session_id, InteractionEventKind.RUN_COMPLETED,
                    {"value": {"interrupted": True}}, origin_channel=origin, run_id=run_id))
            else:
                session.resume_request_id = command.request_id
        finally:
            await manager.close()
        self._ensure_running(command.session_id)
        return CommandReceipt(True, run_id=session.current.run_id if session.current else None)

    def _ensure_running(self, session_id: str) -> None:
        """同步建立唯一 worker 或唤醒已有任务，未激活会话保持不执行。

        Args:
            session_id (str): 需要检查工作的会话。
        """
        session = self._session(session_id)
        if self.phase != RuntimePhase.RUNNING or not session.activated:
            return
        # 1. 检查和创建之间不等待，同一事件循环无需另加 worker 创建锁。
        if session.worker is None or session.worker.done():
            session.worker = asyncio.create_task(self._drain(session_id))
        session.wakeup.set()

    async def _receive_task_records(self, supervisor: TaskSupervisor) -> None:
        """启动后独占读取任务记录，来源结束或最终关闭时退出。

        Args:
            supervisor (TaskSupervisor): 应用注入的共享记录来源，读取不影响其进程生命周期。
        """
        try:
            while True:
                record = await supervisor.next_event()
                if record is None:
                    return
                if self.phase == RuntimePhase.RUNNING:
                    await self._handle_task_record(record)
        except Exception:
            logger.exception("读取任务记录失败")
            raise

    async def _handle_task_record(self, record: TaskRecord) -> None:
        """读取任务记录后更新显示，并按当前执行资格提醒 worker 检查 Inbox。

        Args:
            record (TaskRecord): Supervisor 已保存的状态；终态通知已在同一事务提交。
        """
        async with self._session(record.session_id).lock:
            # 1. 等锁期间可能开始关闭，此时既不发布显示事件，也不激活会话。
            if self.phase != RuntimePhase.RUNNING:
                return
            if record.background:
                self._emit(InteractionEvent(record.session_id, InteractionEventKind.TASK_STATUS,
                    {"task_id": record.task_id, "status": record.status,
                     "exit_code": record.exit_code, "tool_name": record.tool_name}, task_id=record.task_id))
            # 2. 记录仅提示重新检查；输入内容和是否已交付仍由持久 Inbox 决定。
            if record.status != TaskStatus.RUNNING:
                self._ensure_running(record.session_id)

    async def _consume_events(
        self, session_id: str, origin: str, run_id: str, stream: AsyncIterator[AgentEvent],
    ) -> None:
        """把 Agent 输出转为交互事件，直接改变当前轮次并暂存。

        Args:
            session_id (str): 会话编号。
            origin (str): 本轮来源渠道。
            run_id (str): 已开始的回答编号。
            stream (AsyncIterator[AgentEvent]): 本次 Agent 的输出。
        """
        session = self._session(session_id)
        error_reported = False
        try:
            # 显式等待下层迭代关闭，保证响应和借用资源在返回前完成清理。
            async with aclosing(stream) as managed_events:
                async for event in managed_events:
                    async with session.lock:
                        # 1. 业务状态在 Scheduler 内改变，不等待连接或通过通知回调改变。
                        if event.type == EventType.RUN_STARTED:
                            # 1.1. Agent 已加载并核验状态；重试沿用身份，不重复发布开始。
                            current = session.current
                            if current is not None and not current.started:
                                origin = event.data["origin_channel"]
                                current.origin = origin
                                current.started = True
                                self._emit(InteractionEvent(session_id, InteractionEventKind.RUN_STARTED,
                                    {"resumed": True} if event.data["resumed"] else {},
                                    origin_channel=origin, run_id=run_id))
                            continue
                        if event.type == EventType.HUMAN_INPUT_COMMITTED:
                            # 1.2. 只转出已保存真人事实，字段沿用原通知，不改变当前轮次。
                            message = event.data["message"]
                            self._emit(InteractionEvent(session_id, InteractionEventKind.HUMAN_MESSAGE,
                                {"message": message.text,
                                 "images": [part.url for part in message.content if isinstance(part, ImageContent)],
                                 "input_sequence": event.data["input_sequence"], "message_id": message.message_id},
                                origin_channel=message.origin_channel))
                            continue
                        if event.type == EventType.INTERRUPT and session.current:
                            session.current.approval = event.data
                        elif event.type in {EventType.DONE, EventType.ERROR}:
                            session.current = None
                        self._emit(InteractionEvent(
                            session_id, AGENT_TO_INTERACTION_EVENT[event.type], {"value": event.data},
                            origin_channel=origin, run_id=run_id,
                            request_id=(event.data or {}).get("request_id") if event.type == EventType.INTERRUPT else None,
                        ))
                        error_reported = error_reported or event.type == EventType.ERROR
        except Exception:
            # 2. 未报告的异常交给本轮入口处理；已有错误结果时仅记录后续源流清理失败。
            if not error_reported:
                raise
            logger.exception("会话 %s 已报告错误，结束输出时再次失败", session_id)

    async def _drain(self, session_id: str) -> None:
        """检查审批恢复和持久输入，没有工作时等待输入、审批或任务通知。

        Args:
            session_id (str): 本 worker 唯一负责的会话。
        """
        session = self._session(session_id)
        manager = self._manager_factory(session_id)
        try:
            while self.phase == RuntimePhase.RUNNING:
                # 1. 必须在所有工作检查之前 clear；查询期间的新通知不能被事后擦除。
                session.wakeup.clear()
                async with session.lock:
                    # 1.1. 旧轮已经收尾时也检查资格，暂停期间把未交付内容留在 Inbox。
                    if not session.activated:
                        session.worker = None
                        return
                    request_id = session.resume_request_id
                    session.resume_request_id = None
                    batch = () if request_id is not None else await manager.read_activation_batch(
                        kind=InboxKind.HUMAN if session.phase == SessionPhase.PENDING_INTERRUPT else None,
                        include_queued=session.phase != SessionPhase.PENDING_INTERRUPT,
                    )
                # 2. 保存过的审批优先继续；执行期间不持会话锁，命令才能发出停止或引导。
                if request_id is not None or batch:
                    keep_running = await self._execute_turn(session_id, manager, batch, request_id)
                    if not keep_running:
                        async with session.lock:
                            # 2.1. 清理期间真人输入可能已重新激活；此时当前 worker 继续处理。
                            if not session.activated:
                                session.worker = None
                                return
                    continue
                await session.wakeup.wait()
        except Exception as exc:
            logger.exception("会话 %s 的输入读取失败", session_id)
            await self._record_failure(session_id, DESKTOP_CHANNEL, None, exc)
        finally:
            try:
                await manager.close()
            except Exception:
                logger.exception("关闭会话 %s 的输入连接失败", session_id)

    async def _record_failure(self, session_id: str, origin: str, run_id: str | None, exc: Exception) -> None:
        """结束本轮并报告一次执行错误，不把错误作为长期阶段。

        Args:
            session_id (str): 失败会话。
            origin (str): 本轮来源。
            run_id (str | None): 尚未创建 Agent 时可为空。
            exc (Exception): 本轮失败原因。
        """
        session = self._session(session_id)
        async with session.lock:
            session.current = None
            self._emit(InteractionEvent(session_id, InteractionEventKind.ERROR,
                {"detail": str(exc)}, origin_channel=origin, run_id=run_id))

    async def _execute_turn(
        self, session_id: str, manager: SessionStateManager, batch: tuple[InboxEnvelope, ...], request_id: str | None,
    ) -> bool:
        """创建和交付输入，未交付失败最多尝试四次，已交付执行不重跑。

        Args:
            session_id (str): 当前会话。
            manager (SessionStateManager): worker 拥有并借给本次 Agent 的会话状态业务对象。
            batch (tuple[InboxEnvelope, ...]): 准备交付的输入，审批恢复时为空。
            request_id (str | None): 从 checkpoint 恢复的审批请求。

        Returns:
            bool: 是否继续检查工作；用户暂停或重试耗尽后结束自动处理。
        """
        session = self._session(session_id)
        human = next((item for item in batch if item.kind == InboxKind.HUMAN), None)
        terminals = tuple(item for item in batch if item.kind == InboxKind.TASK_TERMINAL)
        origin = human.origin_channel if human else DESKTOP_CHANNEL
        run_id = None
        control = None
        try:
            # 1. 恢复只传请求编号；状态、持久决定和有效性由 Agent 自行加载核验。
            if request_id is not None and session.current is not None:
                origin = session.current.origin
                run_id = session.current.run_id
            attempts = INPUT_DELIVERY_MAX_ATTEMPTS if batch else 1
            for attempt in range(attempts):
                agent = None
                try:
                    # 2. 每次重试创建独立 Agent；首次创建成功才建立轮次，重试沿用身份和控制。
                    async with session.lock:
                        # 2.1. 输入交付重试也遵守用户停止；随后真人输入恢复的资格照常保留。
                        if not session.activated:
                            # 2.1.1. 当前轮次可能已通过事件或状态查询显示为执行中；结束后通知界面，输入留待真人恢复。
                            if session.current is not None:
                                self._emit(InteractionEvent(session_id, InteractionEventKind.RUN_COMPLETED,
                                    {"value": {"interrupted": True}},
                                    origin_channel=session.current.origin, run_id=session.current.run_id))
                                session.current = None
                            return False
                        agent = self._agent_factory(session_id, manager)
                        if control is None:
                            run_id = run_id or uuid4().hex
                            control = TurnControl(run_id)
                            session.current = _CurrentTurn(run_id, origin, control)
                    if request_id is not None:
                        stream = agent.resume(request_id, turn_control=control, receive_task_notifications=True)
                    elif human is not None:
                        stream = agent.run(
                            human.payload["message"], images=human.payload.get("images"), origin_channel=origin,
                            inbox_input=human, inbox_inputs=batch, turn_control=control,
                            receive_task_notifications=True,
                        )
                    else:
                        stream = agent.run_runtime_inputs(inbox_inputs=terminals,
                            turn_control=control, receive_task_notifications=True)
                    await self._consume_events(session_id, origin, run_id, stream)
                    return True
                except Exception as exc:
                    # 3. 仅异常路径核对交付事实；已保存输入不能因为后续执行失败重新运行。
                    item = human or (batch[0] if batch else None)
                    status = await manager.status(item.id) if item is not None else InboxStatus.DELIVERED
                    if status is None:
                        async with session.lock:
                            session.current = None
                            if run_id is not None:
                                self._emit(InteractionEvent(session_id, InteractionEventKind.RUN_COMPLETED,
                                    {"value": {"input_cancelled": True}}, origin_channel=origin, run_id=run_id))
                        return True
                    if status == InboxStatus.DELIVERED:
                        logger.exception("会话 %s 已交付输入后的执行失败", session_id)
                        await self._record_failure(session_id, origin, run_id, exc)
                        return True
                    if attempt + 1 < attempts:
                        logger.warning("会话 %s 输入交付失败，第 %s 次尝试：%s", session_id, attempt + 1, exc)
                        continue
                    # 4. 耗尽后先移除失败真人输入，再一次性报告；任务通知保持待交付事实。
                    async with session.lock:
                        failed = None
                        if human is not None:
                            await manager.discard_human_input(human.id)
                            failed = {"input_sequence": human.id, "message": human.payload["message"],
                                      "images": human.payload.get("images") or []}
                        session.activated = False
                        session.current = None
                        payload = {"detail": str(exc)}
                        if failed is not None:
                            payload["failed_input"] = failed
                        self._emit(InteractionEvent(session_id, InteractionEventKind.ERROR, payload,
                            origin_channel=origin, run_id=run_id))
                    return False
                finally:
                    # 5. 清理异常单独报告到日志，不能触发重试或覆盖已保存结果。
                    if agent is not None:
                        try:
                            await agent.close()
                        except Exception:
                            logger.exception("关闭会话 %s 的 Agent 失败", session_id)
        except Exception as exc:
            logger.exception("会话 %s 的执行准备失败", session_id)
            await self._record_failure(session_id, origin, run_id, exc)
            session.activated = False
            return False
        finally:
            # 6. 关闭取消继续传播，但始终清除旧控制对象；待恢复审批保留原回答身份。
            async with session.lock:
                if session.current and session.current.control is control:
                    session.current.control = None
                    if session.current.approval is None and session.resume_request_id is None:
                        session.current = None

    async def begin_shutdown(self) -> None:
        """封住新命令、捕获正文并请求 worker 取消，不关闭外部共享资源。"""
        async with self._lifecycle_lock:
            if self.phase in {RuntimePhase.STOPPING, RuntimePhase.STOPPED}:
                return
            self.phase = RuntimePhase.STOPPING
            for session in self._sessions.values():
                current = session.current
                if current and current.control:
                    control = current.control
                    session.shutdown_partial = (
                        control.partial, control.partial_persisted, current.origin, control.partial_message_id,
                    )
                if session.worker is not None and not session.worker.done():
                    session.worker.cancel()

    async def wait_for_workers(self) -> None:
        """应用封住入口后等待执行清理，供共享资源关闭前调用。"""
        workers = [session.worker for session in self._sessions.values() if session.worker is not None]
        if workers:
            results = await asyncio.gather(*workers, return_exceptions=True)
            for result in results:
                if isinstance(result, Exception):
                    logger.error("会话任务清理失败", exc_info=(type(result), result, result.__traceback__))
        for session in self._sessions.values():
            session.worker = None

    async def close(self) -> None:
        """等待 worker 和记录消费者，保存终止记录并关闭输出；共享资源仍由应用清理。"""
        await self.begin_shutdown()
        await self.wait_for_workers()
        # 1. 不等待 Supervisor 关闭；独立关闭 Scheduler 也必须结束记录等待或会话锁等待。
        if self._task_reader is not None:
            if not self._task_reader.done():
                self._task_reader.cancel()
            results = await asyncio.gather(self._task_reader, return_exceptions=True)
            for result in results:
                if isinstance(result, Exception):
                    logger.error("任务记录消费者异常退出", exc_info=(type(result), result, result.__traceback__))
            self._task_reader = None
        async with self._lifecycle_lock:
            if self.phase == RuntimePhase.STOPPED:
                return
            try:
                await self._finalize_abandoned_sessions()
                await self._reset_inbox()
                self._sessions.clear()
                self.phase = RuntimePhase.STOPPED
            finally:
                # 2. 保存失败也结束输出等待；关闭期间不承诺交付最后的展示通知。
                self._output_closed = True
                while not self._output.empty():
                    self._output.get_nowait()
                self._output.put_nowait(None)
