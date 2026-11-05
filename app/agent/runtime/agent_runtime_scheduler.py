"""协调各会话的执行，受理运行请求并暂存输出事件。"""

from __future__ import annotations

from contextlib import aclosing

import asyncio
from dataclasses import asdict, replace
import logging
from typing import Callable, Awaitable
from uuid import uuid4

from app.agent.agent import Agent
from app.agent.runtime.agent_session import AgentSession
from app.agent.runtime.approval_queue import ApprovalQueue
from app.services.agent_identity_service import AgentIdentityService
from app.services.subagent_service import SubagentService
from app.agent.runtime.subagent_requests import (CreateSubagentRequest, InteractSubagentRequest, SubagentDetailRequest,
    SubagentSnapshotRequest, ParentMessageRequest, SubagentSource, SubagentReceipt, SubagentRequest)
from app.crud.chat_settings_dao import ChatSettingsDao
from app.services.settings_mutation import SettingsMutationCoordinator
from app.crud.subagent_dao import SubagentDao
from app.crud.task_record_dao import TaskRecordDao
from app.schemas.agent_storage import AgentStorageScope
from app.schemas.subagent_types import (SubagentDefinition, SubagentRecord, SubagentSummary,
    SubagentAction, SubagentCommandEffect, SubagentInteractionKind, TaskRecordKind)
from app.agent.state.manager import SessionStateManager
from app.schemas.task_types import TaskRecord, PluginRequest, PluginRequestKind, TaskStopRequest
from app.agent.runtime.requests import RuntimeRequest
from app.schemas.runtime_types import (
    CommandKind, DESKTOP_CHANNEL, InboxDeliveryMode, InteractionEventKind, SessionPhase,
    TaskStatus, RuntimePhase, InterruptKind,
)
from app.interaction.interaction_contract import ChannelCommand, CommandReceipt, InteractionEvent
from app.agent.runtime.tasks.task_supervisor import TaskSupervisor
from app.services.channel_subscription_service import ChannelSubscriptionService

logger = logging.getLogger(__name__)
AGENT_CLEANUP_RETRY_SECONDS = 1

class AgentRuntimeScheduler:
    """路由命令、登记会话 worker 并保存供渠道分发的临时输出。

    Attributes:
        subscriptions (ChannelSubscriptionService): 命令入口与 Hub 共用的配置。
        task_supervisor (TaskSupervisor | None): 应用持有的共享后台任务管理对象。
        phase (RuntimePhase): 后端执行入口的生命周期。
        _sessions (dict[str, AgentSession]): 每个上下文的唯一执行与资源 owner。
        _output (asyncio.Queue): 由唯一 Hub 读取的临时事件队列。
        _epoch (str): 本次 Scheduler 实例的事件编号标识。
        _lifecycle_lock (asyncio.Lock): 启动、命令受理与开始退出之间的互斥。
        _task_reader (asyncio.Task | None): 独占读取共享队列中任务记录和插件请求的任务。
    """

    def __init__(
        self, *, runtime_queue: asyncio.Queue[RuntimeRequest], agent_factory: Callable[[str, SessionStateManager], Agent],
        subscriptions: ChannelSubscriptionService | None = None,
        session_state_manager_factory: Callable[[str], SessionStateManager] = SessionStateManager,
        task_supervisor: TaskSupervisor | None = None,
        subagent_factory: Callable[[SubagentDefinition, SessionStateManager], Agent] | None = None,
        subagent_dao: SubagentDao | None = None,
        chat_settings_dao: ChatSettingsDao | None = None,
        mutation_coordinator: SettingsMutationCoordinator | None = None,
        cleanup_session_connections: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        """保存执行依赖，实际 worker 在受理业务后登记。

        Args:
            agent_factory (Callable[[str, SessionStateManager], Agent]): 按会话创建本次 Agent。
            subscriptions (ChannelSubscriptionService | None): 应用注入的共享配置，测试可省略。
            session_state_manager_factory (Callable[[str], SessionStateManager]): 当前会话的状态业务对象工厂。
            task_supervisor (TaskSupervisor | None): 共享任务查询与停止入口。
            runtime_queue (asyncio.Queue[RuntimeRequest]): 应用创建、Scheduler 独占消费的共享请求队列。
            chat_settings_dao (ChatSettingsDao | None): 父配置存在性来源，省略时复用 Supervisor 的依赖。
            mutation_coordinator (SettingsMutationCoordinator | None): 与删除和普通任务登记共用的受理短锁。
            cleanup_session_connections (Callable | None): 父后台清理最后释放会话连接的装配回调。
        """
        self.subscriptions = subscriptions or ChannelSubscriptionService()
        self.task_supervisor = task_supervisor
        self.runtime_queue = runtime_queue
        self._agent_factory = agent_factory
        self._subagent_factory = subagent_factory
        self._subagent_dao = subagent_dao
        self._subagent_records: dict[str, SubagentRecord] = {}
        self._approval_queue = ApprovalQueue()
        self._subagent_results: TaskRecordDao | None = None
        self._settings_dao = chat_settings_dao or (task_supervisor.settings_dao if task_supervisor else ChatSettingsDao())
        self._mutation_coordinator = mutation_coordinator or (task_supervisor.mutation_coordinator if task_supervisor else SettingsMutationCoordinator())
        self._cleanup_session_connections = cleanup_session_connections
        self._parent_cleanup_workers: dict[str, asyncio.Task[None]] = {}
        self._manager_factory = session_state_manager_factory
        if self._subagent_dao is None:
            # 工厂对象尚未打开数据库，仅据其隔离路径装配共用存储。
            self._subagent_dao = (task_supervisor.identity.subagents if task_supervisor else
                                 SubagentDao(self._manager_factory("__subagent_init__").db_path))
        self._subagent_results = task_supervisor.records if task_supervisor else TaskRecordDao(self._subagent_dao.db_path)
        self._identity = (task_supervisor.identity if task_supervisor and task_supervisor.identity.subagents is self._subagent_dao
                          and task_supervisor.identity.settings is self._settings_dao else
                          AgentIdentityService(self._settings_dao, self._subagent_dao))
        self._subagents = SubagentService(self._subagent_dao, self._subagent_results, self._identity)
        self._sessions: dict[str, AgentSession] = {}
        self._output: asyncio.Queue[InteractionEvent | None] = asyncio.Queue()
        self._output_closed = False
        self._epoch = uuid4().hex
        self._lifecycle_lock = asyncio.Lock()
        self._task_reader: asyncio.Task[None] | None = None
        self.phase = RuntimePhase.INITIALIZING

    def _session(self, session_id: str) -> AgentSession:
        """取得或创建内存记录；读取记录不会获得执行资格。

        Args:
            session_id (str): 会话编号。

        Returns:
            AgentSession: 此会话唯一的内存记录。
        """
        if session_id not in self._sessions:
            self._sessions[session_id] = AgentSession(session_id, identity=self._identity,
                mutations=self._mutation_coordinator, subagents=self._subagents, agent_factory=self._agent_factory,
                subagent_factory=self._subagent_factory, manager_factory=self._manager_factory,
                task_supervisor=self.task_supervisor, runtime_queue=self.runtime_queue)
        return self._sessions[session_id]

    def _emit(self, event: InteractionEvent) -> None:
        """同步编号并暂存事件，状态修改调用方必须已持会话锁。

        状态变化到入队在当前协程内同步完成，后续由 Hub 读取和分发。

        Args:
            event (InteractionEvent): 已完成前置保存的业务通知。
        """
        if self._output_closed:
            return
        if not self._settings_dao.exists(event.session_id):
            return
        session = self._session(event.session_id)
        session.sequence += 1
        self._output.put_nowait(replace(event, stream_epoch=self._epoch, event_sequence=session.sequence))

    async def _route_session_event(self, event: InteractionEvent) -> None:
        """根据已提交事件路由接收方，并推进全局审批展示顺序。"""
        child = self._subagent_records.get(event.session_id)
        parent_id = child.definition.session_id if child else event.session_id
        if not self._settings_dao.exists(parent_id):
            return
        if event.kind == InteractionEventKind.INTERRUPT:
            value = event.payload.get("value") or {}
            if value.get("type") == InterruptKind.PARENT_MESSAGE:
                self._ensure_running(parent_id)
                return
            if event.request_id:
                self._approval_queue.enqueue(parent_id, event.session_id, event.request_id)
                if child:
                    self._show_next_approval(parent_id)
                elif self._approval_queue.head(parent_id) == (event.session_id, event.request_id):
                    self._emit(event)
            return
        if event.kind == InteractionEventKind.AUTHORIZATION_RESOLVED:
            self._approval_queue.remove(parent_id, event.session_id, event.request_id)
            if child:
                event = replace(event, session_id=parent_id, payload={**event.payload, "subagent_id": child.definition.subagent_id})
            self._emit(event)
            self._show_next_approval(parent_id)
            return
        if child is None:
            self._emit(event)

    def _approval_prompt(self, session_id: str) -> dict[str, object] | None:
        """读取队首的实际审批材料，重连也只展示这一项。"""
        if not self._settings_dao.exists(session_id):
            return None
        entry = self._approval_queue.head(session_id)
        session = self._sessions.get(entry[0]) if entry is not None else None
        if session is None or session.current is None or session.current.approval is None:
            return None
        return {"value": session.current.approval,
                "subagent_id": entry[0] if entry[0] != session_id else None,
                "request_id": entry[1]}

    def _show_next_approval(self, session_id: str) -> None:
        """当前请求决定或取消后，展示队首上下文保存的下一项审批。"""
        prompt = self._approval_prompt(session_id)
        if prompt is not None:
            kind = InteractionEventKind.SUBAGENT_INTERRUPT if prompt["subagent_id"] else InteractionEventKind.INTERRUPT
            self._emit(InteractionEvent(session_id, kind, prompt, request_id=prompt["request_id"]))

    async def next_event(self) -> InteractionEvent | None:
        """供 Hub 独占读取临时输出，对迟到的身份相关通知再次核对资格。

        Returns:
            InteractionEvent | None: 下一条事件；输出关闭且无剩余数据时为空。
        """
        while True:
            if self._output_closed and self._output.empty():
                return None
            event = await self._output.get()
            if event is None:
                return None
            if not self._settings_dao.exists(event.session_id):
                continue
            subagent_id = event.payload.get("subagent_id")
            if subagent_id and not event.payload.get("removed") and event.kind != InteractionEventKind.AUTHORIZATION_RESOLVED:
                record = await self._subagent_dao.get(event.session_id, str(subagent_id))
                if record is None or record.retired_at is not None:
                    continue
            return event

    #=====================子身份登记和持久详情======================

    async def _load_subagents(self) -> None:
        """启动时整理遗留执行记录，将可复用身份加载为空闲状态。"""
        available = await self._subagents.recover()
        self._subagent_records = {item.definition.subagent_id: item for item in available}
        if self.task_supervisor is not None:
            for record in await self._subagent_dao.list_records():
                if record.retired_at is not None:
                    await self.task_supervisor.stop_subagent_tasks(record.definition.session_id, record.definition.subagent_id)

    def _subagent_summary(self, record: SubagentRecord) -> SubagentSummary:
        """从子定义及 Session 当前执行事实生成公开状态。"""
        return self._session(record.definition.subagent_id).snapshot(record.definition)

    async def snapshot_subagents(self, session_id: str) -> tuple[SubagentSummary, ...]:
        """供环境及前端读取当前父会话下的全部可用子摘要。"""
        if not self._settings_dao.exists(session_id):
            return ()
        return tuple(self._subagent_summary(record) for record in await self._subagent_dao.list_available(session_id))

    async def get_subagent_details(self, session_id: str, subagent_id: str,
                                   interaction_limit: int = 10) -> dict[str, object]:
        """按归属查询定义、真实状态、显式交互及最终段结果，释放后仍可查询。"""
        result = await self._subagents.get_details(session_id, subagent_id, interaction_limit)
        record = await self._subagent_dao.get(session_id, subagent_id)
        result["status"] = self._subagent_summary(record).status.value if result["context_available"] else None
        return result

    def _check_subagent_source(self, source: SubagentSource, *, child: bool = False) -> None:
        """核对固定来源和当前执行，子调用只能进入专用父通信路径。"""
        if self.phase != RuntimePhase.RUNNING:
            raise RuntimeError("运行时正在关闭")
        if child != (source.subagent_id is not None):
            raise ValueError("子 Agent 不能创建、管理或查询其他 Agent")
        if not child and source.session_id in self._subagent_records:
            raise ValueError("子 Agent 不能使用父管理入口")
        if not self._settings_dao.exists(source.session_id):
            raise RuntimeError("父 Agent 配置已删除")
        context_id = source.subagent_id or source.session_id
        session = self._sessions.get(context_id)
        if session is None or session.current is None or session.current.run_id != source.run_id:
            raise ValueError("调用来源执行已结束")
        if child:
            record = self._subagent_records.get(context_id)
            if record is None or record.definition.session_id != source.session_id:
                raise ValueError("父子归属不匹配")

    async def _accept_subagent_request(self, request: SubagentRequest) -> (
        SubagentReceipt | dict[str, object] | tuple[SubagentSummary, ...] | None
    ):
        """唯一消费者只受理及读取数据，后台执行和回复等待由各自协程持有。"""
        if self.phase != RuntimePhase.RUNNING:
            raise RuntimeError("运行时正在关闭")
        if isinstance(request, SubagentSnapshotRequest):
            return await self.snapshot_subagents(request.session_id)
        if isinstance(request, InteractSubagentRequest):
            async with self._mutation_coordinator.admission_lock:
                self._check_subagent_source(request.source)
                return await self._interact_subagent(request.source.session_id, request.subagent_id, request.action,
                    request.text, source_id=f"{request.source.run_id}:{request.source.tool_call_id}")
        async with self._mutation_coordinator.admission_lock:
            self._check_subagent_source(request.source, child=isinstance(request, ParentMessageRequest))
            if isinstance(request, CreateSubagentRequest):
                if self._subagent_factory is None:
                    raise RuntimeError("未装配子 Agent 工厂")
                definition, created = await self._subagents.create(request)
                self._subagent_records[definition.subagent_id] = SubagentRecord(definition)
                target = self._session(definition.subagent_id)
                receipt = await target.accept_created_identity(definition, created=created)
                self._ensure_running(definition.subagent_id)
                return receipt
            if isinstance(request, SubagentDetailRequest):
                return await self.get_subagent_details(request.source.session_id, request.subagent_id, request.interaction_limit)
            record = await self._subagent_dao.get(request.source.session_id, request.source.subagent_id)
            await self._identity.require_available(request.source.session_id, request.source.subagent_id)
            _, created = await self._subagent_dao.append_message(record.definition, SubagentInteractionKind.A2A,
                request.text, request.request_id, request_id=request.request_id)
            if created:
                self._ensure_running(request.source.session_id)
            return None

    #=====================子业务路由======================

    async def interact_subagent(self, session_id: str, subagent_id: str, action: SubagentAction,
                                text: str | None = None, *, source_id: str | None = None) -> SubagentReceipt:
        """在目标锁下受理命令、停止或销毁，执行和资源清理由 worker 完成。

        Args:
            session_id (str): 父配置所属会话，用于校验目标归属。
            subagent_id (str): 要交互的子身份。
            action (SubagentAction): 发送命令、停止当前工作或销毁整个上下文。
            text (str | None): command 的正文，按实际状态作为委派、引导或 A2A 回复。
            source_id (str | None): 父工具调用来源；用户界面操作由此处生成独立来源编号。

        Returns:
            SubagentReceipt: 已受理动作及当时状态，执行结果随后通过父 Inbox 交付。

        Raises:
            ValueError: 目标不属于父会话、操作无效或命令为空。
        """
        async with self._lifecycle_lock, self._mutation_coordinator.admission_lock:
            if self.phase != RuntimePhase.RUNNING:
                raise ValueError("运行时尚未就绪或正在关闭")
            if not self._settings_dao.exists(session_id):
                raise ValueError("父 Agent 配置已删除")
            return await self._interact_subagent(session_id, subagent_id, action, text, source_id=source_id)

    #=====================复用 Agent 执行和统一子收尾======================

    async def _interact_subagent(self, session_id: str, subagent_id: str, action: SubagentAction,
                                 text: str | None = None, *, source_id: str | None = None) -> SubagentReceipt:
        """统一归属查询后调用完整会话操作，并登记唯一 worker。"""
        record = await self._subagent_dao.get(session_id, subagent_id)
        if record is None:
            raise ValueError("子 Agent 不存在或不属于当前会话")
        action = SubagentAction(action)
        session = self._session(subagent_id)
        if record.retired_at is not None and (action != SubagentAction.DESTROY or session.subagent is None):
            return SubagentReceipt(subagent_id, action == SubagentAction.DESTROY, SubagentCommandEffect.RELEASED, None)
        self._subagent_records[subagent_id] = record
        if action == SubagentAction.COMMAND:
            receipt = await session.accept_command(record.definition, text, source_id or uuid4().hex)
        elif action == SubagentAction.STOP:
            receipt = await session.request_stop(record.definition)
        else:
            receipt = await session.destroy(record.definition)
        self._ensure_running(subagent_id)
        return receipt

    async def _resolve_authorization(self, command: ChannelCommand) -> CommandReceipt:
        """将审批决定交给实际会话，展示队列只根据已提交事件推进。"""
        result = await self._session(command.session_id).resolve_authorization(command)
        self._ensure_running(command.session_id)
        return result

    async def _drive_session(self, session: AgentSession) -> None:
        """驱动唯一会话事件流，执行结束后按当前受理资格交接 worker。"""
        try:
            async with aclosing(session.run()) as stream:
                async for event in stream:
                    await self._route_session_event(event)
        finally:
            if session.worker is asyncio.current_task():
                session.worker = None
                self._ensure_running(session.context_id)

    def schedule_parent_cleanup(self, session_id: str) -> None:
        """配置删除提交后登记唯一后台清理；调用方立即返回成功。

        Args:
            session_id (str): 配置已经删除的父身份。
        """
        if self._settings_dao.exists(session_id):
            raise ValueError("父配置仍存在，不能清理父 Agent")
        previous = self._parent_cleanup_workers.get(session_id)
        if previous is None or previous.done():
            self._parent_cleanup_workers[session_id] = asyncio.create_task(self._cleanup_parent(session_id))

    async def _cleanup_parent(self, session_id: str) -> None:
        """停止所属执行、补齐子废弃及历史，最后释放会话连接；失败每秒重试。"""
        while True:
            try:
                # 1. 已删除的父身份不再接收工作，先结束原 worker 以归还 Agent 资源。
                parent = self._sessions.get(session_id)
                if parent is not None:
                    await parent.finish_execution_cleanup()
                    self._approval_queue.remove(session_id, session_id)
                # 2. 父配置缺失已经封闭全部子入口；逐一提交 SQL 废弃并等待原清理 worker。
                for record in await self._subagent_dao.list_records(session_id):
                    identifier = record.definition.subagent_id
                    async with self._mutation_coordinator.admission_lock:
                        await self._interact_subagent(session_id, identifier, SubagentAction.DESTROY)
                    child = self._sessions.get(identifier)
                    if child is not None and child.worker is not None:
                        await asyncio.shield(child.worker)
                        if child.subagent is not None:
                            raise RuntimeError("子资源尚未释放")
                if self.task_supervisor is not None:
                    await self.task_supervisor.stop_session_tasks(session_id)
                manager = self._manager_factory(session_id)
                partial = (parent.shutdown_partial if parent is not None else None) or ("", False, DESKTOP_CHANNEL, None)
                try:
                    await manager.finalize_abandoned_turn(
                        partial=partial[0], partial_persisted=partial[1],
                        origin_channel=partial[2], partial_message_id=partial[3],
                    )
                    await manager.discard_pending_inputs()
                finally:
                    await manager.close()
                # 3. Agent、普通任务均已退出，再释放该父会话持有的插件 MCP 连接。
                if self._cleanup_session_connections is not None:
                    await self._cleanup_session_connections(session_id)
                return
            except Exception:
                logger.exception("父 Agent 后台清理失败，将自动重试：%s", session_id)
                if self.phase != RuntimePhase.RUNNING:
                    return
                await asyncio.sleep(AGENT_CLEANUP_RETRY_SECONDS)
                if self.phase != RuntimePhase.RUNNING:
                    return

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
            await self._load_subagents()
            self.phase = RuntimePhase.RUNNING
            self._task_reader = asyncio.create_task(self._receive_runtime_requests())

    async def _reset_inbox(self) -> None:
        """作废旧真人意图，并清除配置已删除会话的全部待交付输入。"""
        manager = self._manager_factory("__runtime_init__")
        try:
            await manager.reset_after_restart()
            pending_sessions = await manager.pending_sessions()
        finally:
            await manager.close()
        for session_id in pending_sessions:
            if self._settings_dao.exists(session_id):
                continue
            manager = self._manager_factory(session_id)
            try:
                await manager.discard_pending_inputs()
            finally:
                await manager.close()

    async def _finalize_abandoned_sessions(self) -> None:
        """为启动或退出时遗留的轮次补齐工具协议并保存结束状态。"""
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
        """先复制阶段和事件编号，再查询持久输入及任务，生成重连快照。

        Args:
            session_id (str): 共享会话编号。

        Returns:
            InteractionEvent: 会话阶段快照及后续数据库查询结果，供 Hub 按渠道合并增量。
        """
        session = self._session(session_id)
        async with session.lock:
            # 1. 状态和事件编号一起复制，数据库等待期间可继续产生更新事件。
            current = session.current if session.phase != SessionPhase.IDLE else None
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
            [asdict(item) for item in await self.task_supervisor.list_tasks(session_id)]
            if self.task_supervisor is not None else []
        )
        payload["subagents"] = [asdict(item) for item in await self.snapshot_subagents(session_id)]
        payload["approval_prompt"] = self._approval_prompt(session_id)
        return InteractionEvent(
            session_id, InteractionEventKind.SESSION_SNAPSHOT, payload,
            stream_epoch=self._epoch, event_sequence=sequence,
        )

    async def submit(self, command: ChannelCommand) -> CommandReceipt:
        """检查生命周期、渠道，再在会话锁内受理命令。

        Args:
            command (ChannelCommand): 渠道已经归一化的用户动作。

        Returns:
            CommandReceipt: 受理结果及已有轮次编号，新轮编号在 worker 开始执行时生成。
        """
        async with self._lifecycle_lock, self._mutation_coordinator.admission_lock:
            if self.phase != RuntimePhase.RUNNING:
                return CommandReceipt(False, "运行时尚未就绪或正在关闭")
            if not self._settings_dao.exists(command.session_id):
                return CommandReceipt(False, "父 Agent 配置已删除")
            try:
                self.subscriptions.validate_source(command.origin_channel)
                if not await self.subscriptions.is_enabled(command.session_id, command.origin_channel):
                    raise ValueError("该会话未订阅此渠道")
            except ValueError as exc:
                return CommandReceipt(False, str(exc))
            # 1. 子业务交给目标 Session，停止普通任务交给 Supervisor；分别校验实际目标归属。
            if command.kind == CommandKind.INTERACT_SUBAGENT:
                if not command.subagent_id:
                    return CommandReceipt(False, "缺少子 Agent 编号")
                try:
                    receipt = await self._interact_subagent(command.session_id, command.subagent_id,
                        SubagentAction(command.payload.get("action", SubagentAction.STOP)), command.payload.get("text"))
                except ValueError as exc:
                    return CommandReceipt(False, str(exc))
                return CommandReceipt(receipt.accepted, None if receipt.accepted else receipt.effect)
            if command.kind == CommandKind.RESOLVE_AUTHORIZATION and command.subagent_id:
                record = await self._subagent_dao.get(command.session_id, command.subagent_id)
                if record is None or record.retired_at is not None:
                    return CommandReceipt(False, "子 Agent 不存在或不属于当前会话")
                async with self._session(command.subagent_id).lock:
                    return await self._resolve_authorization(replace(command, session_id=command.subagent_id))
            if command.kind == CommandKind.STOP_BACKGROUND_TASK:
                return await self._stop_background_task(command)
            handlers = {
                CommandKind.CANCEL_QUEUED: self._session(command.session_id).cancel_queued,
                CommandKind.STOP_TURN: self._session(command.session_id).stop_current_turn,
                CommandKind.RESOLVE_AUTHORIZATION: self._resolve_authorization,
                CommandKind.HUMAN_TURN: self._session(command.session_id).accept_human,
                CommandKind.HUMAN_STEER: self._session(command.session_id).accept_human,
                CommandKind.QUEUE_TURN: self._session(command.session_id).accept_human,
            }
            handler = handlers.get(command.kind)
            if handler is None:
                return CommandReceipt(False, "当前命令尚未接入运行时")
            # 2. 固定生命周期锁到会话锁的顺序；worker 不取得生命周期锁。
            async with self._session(command.session_id).lock:
                receipt = await handler(command)
                self._ensure_running(command.session_id)
                return receipt

    async def _stop_background_task(self, command: ChannelCommand) -> CommandReceipt:
        """核对普通后台任务归属后，交给 Supervisor 请求停止。

        Args:
            command (ChannelCommand): 已验证来源的停止任务命令。

        Returns:
            CommandReceipt: Supervisor 是否受理停止。
        """
        if self.task_supervisor is None or not command.task_id:
            return CommandReceipt(False, "后台任务不存在")
        record = await self.task_supervisor.get_task_record(command.task_id)
        if record is None or record.session_id != command.session_id:
            return CommandReceipt(False, "后台任务不存在")
        return CommandReceipt(await self.task_supervisor.stop_task(command.task_id))

    def _ensure_running(self, session_id: str) -> None:
        """为已取得执行资格的上下文登记唯一 worker，或唤醒已有 worker。

        Args:
            session_id (str): 需要检查工作的会话。
        """
        session = self._session(session_id)
        if self.phase != RuntimePhase.RUNNING:
            return
        if session.subagent is None and (not session.activated or not self._settings_dao.exists(session_id)):
            return
        if session.worker is None or session.worker.done():
            session.worker = asyncio.create_task(self._drive_session(session))
        session.wakeup.set()

    async def _receive_runtime_requests(self) -> None:
        """独占消费应用队列，逐项路由请求、发布已提交事件并记录单项错误。"""
        while True:
            request = await self.runtime_queue.get()
            try:
                if request is None:
                    return
                if isinstance(request, InteractionEvent):
                    await self._route_session_event(request)
                elif isinstance(request, TaskRecord):
                    await self._handle_task_record(request)
                elif isinstance(request, TaskStopRequest):
                    # 1. 工具只投递请求；关闭及会话归属检查由唯一消费者受理。
                    async with self._lifecycle_lock:
                        if self.phase != RuntimePhase.RUNNING:
                            raise RuntimeError("运行时正在关闭")
                        if self.task_supervisor is None:
                            raise RuntimeError("任务管理器未装配")
                        record = await self.task_supervisor.get_task_record(request.task_id)
                        if record is None or record.session_id != request.session_id or (
                                request.subagent_id is not None and record.subagent_id != request.subagent_id):
                            raise ValueError("任务不存在或不属于当前聊天")
                        await self.task_supervisor.stop_task(request.task_id)
                elif isinstance(request, (CreateSubagentRequest, InteractSubagentRequest, SubagentDetailRequest,
                                          SubagentSnapshotRequest, ParentMessageRequest)):
                    try:
                        async with self._lifecycle_lock:
                            value = await self._accept_subagent_request(request)
                        if not request.reply.done():
                            request.reply.set_result(value)
                    except asyncio.CancelledError:
                        if not request.reply.done():
                            request.reply.set_exception(RuntimeError("运行时关闭，请求受理已中断"))
                        raise
                    except Exception as exc:
                        if not request.reply.done():
                            request.reply.set_exception(exc)
                else:
                    await self._accept_plugin_request(request)
            except Exception:
                logger.exception("运行时请求处理失败：%s", request)
            finally:
                self.runtime_queue.task_done()

    async def _accept_plugin_request(self, request: PluginRequest) -> None:
        """在既有锁顺序内校验插件来源，保存内容或复用轮次控制规则。

        Args:
            request (PluginRequest): 本次 HTTP 请求或绑定来源的插件请求。
        """
        async with self._lifecycle_lock, self._mutation_coordinator.admission_lock:
            if self.phase != RuntimePhase.RUNNING:
                logger.info("插件请求未受理：运行时正在关闭")
                return
            if not self._settings_dao.exists(request.session_id):
                return
            if not request.plugin_name or not request.session_id:
                raise ValueError("插件请求缺少来源")
            if request.kind == PluginRequestKind.STOP_TASK:
                if self.task_supervisor is not None:
                    record = await self.task_supervisor.get_task_record(request.task_id)
                    if record and record.session_id == request.session_id and record.plugin_name == request.plugin_name:
                        await self.task_supervisor.stop_task(record.task_id)
                    else:
                        logger.info("插件停止任务未受理：任务不存在或来源不匹配")
                return
            session = self._session(request.session_id)
            await session.accept_plugin(request)
            self._ensure_running(request.session_id)

    async def _handle_task_record(self, record: TaskRecord) -> None:
        """读取任务记录后更新显示，并按当前执行资格提醒 worker 检查 Inbox。

        Args:
            record (TaskRecord): Supervisor 已保存的状态；终态通知已在同一事务提交。
        """
        async with self._session(record.session_id).lock:
            # 1. 退出时共享存储可能已经关闭，确认运行资格后再读取身份及展示材料。
            if self.phase != RuntimePhase.RUNNING:
                return
            if not self._settings_dao.exists(record.session_id):
                return
            if record.subagent_id is not None and record.kind != TaskRecordKind.SUBAGENT:
                child = await self._subagent_dao.get(record.session_id, record.subagent_id)
                if child is None or child.retired_at is not None:
                    return
            if record.background:
                payload: dict[str, object] = {"task_id": record.task_id, "status": record.status,
                    "exit_code": record.exit_code, "tool_name": record.tool_name}
                if record.subagent_id is not None:
                    # 1.1. 保留子来源，出队时按最新废弃记录过滤读查询期间产生的迟到状态。
                    payload["subagent_id"] = record.subagent_id
                self._emit(InteractionEvent(record.session_id, InteractionEventKind.TASK_STATUS,
                    payload, task_id=record.task_id))
            # 2. 记录仅提示重新检查；输入内容和是否已交付仍由持久 Inbox 决定。
            if record.status != TaskStatus.RUNNING and record.notify_agent:
                target = record.notification_context_id or record.session_id
                if record.notification_scope == AgentStorageScope.MAIN:
                    self._ensure_running(target)
                else:
                    child = self._sessions.get(target)
                    if child is not None and child.subagent is not None:
                        child.wakeup.set()

    async def begin_shutdown(self) -> None:
        """封住新命令，取消挂起执行并等待已开始的最终提交，共享资源稍后关闭。"""
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
                session.request_shutdown(application_exit=True)

    async def wait_for_workers(self) -> None:
        """应用封住入口后等待执行清理，供共享资源关闭前调用。"""
        workers = [session.worker for session in self._sessions.values() if session.worker is not None]
        workers.extend(self._parent_cleanup_workers.values())
        if workers:
            results = await asyncio.gather(*workers, return_exceptions=True)
            for result in results:
                if isinstance(result, Exception):
                    logger.error("会话任务清理失败", exc_info=(type(result), result, result.__traceback__))
        for session in self._sessions.values():
            session.worker = None
            try:
                await session.finish_execution_cleanup()
            except Exception:
                logger.exception("退出时释放会话资源失败：%s", session.context_id)

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
        # 2. 独立关闭时也结束尚未读取的进程内回执；应用正常退出会先排空生产者队列。
        while not self.runtime_queue.empty():
            request = self.runtime_queue.get_nowait()
            if isinstance(request, (CreateSubagentRequest, InteractSubagentRequest, SubagentDetailRequest,
                                    SubagentSnapshotRequest, ParentMessageRequest)) and not request.reply.done():
                request.reply.set_exception(RuntimeError("运行时已关闭，请求未受理"))
            self.runtime_queue.task_done()
        async with self._lifecycle_lock:
            if self.phase == RuntimePhase.STOPPED:
                return
            try:
                await self._finalize_abandoned_sessions()
                await self._reset_inbox()
                if self.task_supervisor is None or self._subagent_dao is not self.task_supervisor.identity.subagents:
                    await self._subagent_dao.close()
                if self.task_supervisor is None and self._subagent_results is not None:
                    await self._subagent_results.close()
                self._sessions.clear()
                self.phase = RuntimePhase.STOPPED
            finally:
                # 2. 保存失败也结束输出等待；关闭期间不承诺交付最后的展示通知。
                self._output_closed = True
                while not self._output.empty():
                    self._output.get_nowait()
                self._output.put_nowait(None)
