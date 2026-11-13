"""管理应用执行入口、独立父子会话及唯一运行队列消费者。"""

from __future__ import annotations
from app.schemas.runtime_types import CommandReceipt

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import asdict, replace
import logging
from uuid import uuid4
from typing import cast

from app.agent.runtime.approval_queue import ApprovalQueue
from app.agent.runtime.parent_session import ParentSession
from app.agent.runtime.subagent_session import SubagentSession
from app.agent.runtime.requests import ApprovalInvalidated, ExecutionEvent, SessionClosed, SnapshotRequest, RuntimeRequest
from app.agent.runtime.subagent_requests import (
    CreateSubagentRequest, InteractSubagentRequest, ParentMessageRequest, SubagentDetailRequest,
    SubagentRequest, SubagentSnapshotRequest, SubagentSource,
)
from app.agent.runtime.tasks.task_supervisor import TaskSupervisor
from app.agent.state.manager import SessionStateManager
from app.interaction.interaction_contract import ChannelCommand, InteractionEvent
from app.interaction.runtime_events import to_interaction_event
from app.schemas.agent_storage import AgentStorageScope
from app.schemas.runtime_types import CommandKind, CommandStatus, InboxDeliveryMode, InteractionEventKind, InterruptKind, RuntimePhase, TaskExecutionPhase, TaskStatus
from app.schemas.subagent_types import SubagentAction, SubagentDefinition, SubagentInteractionKind, SubagentRecord, SubagentSummary, TaskRecordKind
from app.schemas.task_types import PluginRequest, PluginRequestKind, TaskRecord, TaskStopRequest
from app.services.agent_identity_service import AgentIdentityService
from app.services.channel_subscription_service import ChannelSubscriptionService
from app.services.settings_mutation import SettingsMutationCoordinator
from app.services.subagent_service import SubagentService


logger = logging.getLogger(__name__)


class AgentRuntimeScheduler:
    """决定本次应用是否接受工作，调用会话公开操作并统一转交展示事件。

    Attributes:
        phase (RuntimePhase): 应用入口的唯一运行阶段。
        _sessions (dict[str, ParentSession | SubagentSession]): 按实际 context_id 登记的会话。
        _task_reader (asyncio.Task | None): 应用唯一请求及事件消费者。
        _approval_queue (ApprovalQueue): 父会话内审批展示顺序，材料由会话摘要提供。
    """

    def __init__(self, *, runtime_queue: asyncio.Queue[RuntimeRequest],
                 parent_factory: Callable[[str], ParentSession],
                 child_factory: Callable[[SubagentDefinition], SubagentSession],
                 manager_factory: Callable[[str], SessionStateManager],
                 identity: AgentIdentityService, subagents: SubagentService,
                 task_supervisor: TaskSupervisor, subscriptions: ChannelSubscriptionService,
                 mutation_coordinator: SettingsMutationCoordinator,
                 publish: Callable[[InteractionEvent], InteractionEvent],
                 watermark: Callable[[str], tuple[str, int]],
                 cleanup_session_connections: Callable[[str], Awaitable[None]] | None = None) -> None:
        """接收应用已装配的共享依赖，运行对象保持明确的所有权。

        Args:
            runtime_queue (asyncio.Queue): 生产者共用、由本对象消费的队列。
            parent_factory (Callable): 构造父会话和其独立执行对象。
            child_factory (Callable): 构造子会话和其独立执行对象。
            manager_factory (Callable): 为启动整理创建短期状态对象。
            identity (AgentIdentityService): 显式命令的身份查询。
            subagents (SubagentService): 应用持有的子持久业务。
            task_supervisor (TaskSupervisor): 普通任务执行及查询入口。
            subscriptions (ChannelSubscriptionService): 渠道配置。
            mutation_coordinator (SettingsMutationCoordinator): 配置删除和命令受理的短操作协调。
            publish (Callable): Hub 提供的同步发布能力。
            watermark (Callable): Hub 提供的只读输出水位。
            cleanup_session_connections (Callable | None): 父删除后释放所属共享连接。
        """
        self.runtime_queue, self.task_supervisor, self.subscriptions = runtime_queue, task_supervisor, subscriptions
        self._parent_factory, self._child_factory, self._manager_factory = parent_factory, child_factory, manager_factory
        self._identity, self._subagents = identity, subagents
        self._subagent_dao, self._settings_dao = subagents.definitions, identity.settings
        self._mutation_coordinator = mutation_coordinator
        self._publish, self._watermark = publish, watermark
        self._cleanup_session_connections = cleanup_session_connections
        self._sessions: dict[str, ParentSession | SubagentSession] = {}
        self._subagent_records: dict[str, SubagentRecord] = {}
        self._approval_queue = ApprovalQueue()
        self._parent_cleanup_workers: dict[str, asyncio.Task[None]] = {}
        self._lifecycle_lock = asyncio.Lock()
        self._task_reader: asyncio.Task[None] | None = None
        self.phase = RuntimePhase.INITIALIZING

    def _parent(self, context_id: str) -> ParentSession:
        """取得独立父会话，查询本身保持初始暂停政策。

        Args:
            context_id (str): 父配置和执行的上下文编号。
        """
        session = self._sessions.get(context_id)
        if session is None:
            session = self._parent_factory(context_id)
            self._sessions[context_id] = session
        if not isinstance(session, ParentSession):
            raise ValueError("目标是子上下文")
        return session

    def _child(self, definition: SubagentDefinition) -> SubagentSession:
        """按实际子编号登记独立会话，父子共享代码且状态分别持有。

        Args:
            definition (SubagentDefinition): 已确认父子归属的定义。
        """
        session = self._sessions.get(definition.subagent_id)
        if session is None:
            session = self._child_factory(definition)
            self._sessions[definition.subagent_id] = session
        if not isinstance(session, SubagentSession):
            raise ValueError("目标是父上下文")
        return session

    def _approval_prompt(self, session_id: str) -> dict[str, object] | None:
        """由队首身份读取会话提供的审批展示材料。

        Args:
            session_id (str): 所属父会话。
        """
        entry = self._approval_queue.head(session_id)
        if entry is None:
            return None
        session = self._sessions.get(entry[0]) if entry else None
        value = session.snapshot()["interrupt"] if session else None
        return {"value": value, "subagent_id": entry[0] if entry[0] != session_id else None,
                "request_id": entry[1]} if value else None

    def _show_next_approval(self, session_id: str) -> None:
        """当前审批结束后展示下一项已保存审批。

        Args:
            session_id (str): 所属父会话。
        """
        prompt = self._approval_prompt(session_id)
        if prompt:
            kind = InteractionEventKind.SUBAGENT_INTERRUPT if prompt["subagent_id"] else InteractionEventKind.INTERRUPT
            self._publish(InteractionEvent(session_id, kind, prompt, request_id=str(prompt["request_id"])))

    async def _route_event(self, value: ExecutionEvent | InteractionEvent) -> None:
        """消费业务通知并统一转换发布，Hub 只接收最终展示事实。

        Args:
            value (ExecutionEvent | InteractionEvent): 生产者已经确定的事件。
        """
        event = to_interaction_event(value)
        child = self._subagent_records.get(event.session_id)
        parent_id = child.definition.session_id if child else event.session_id
        if not self._settings_dao.exists(parent_id):
            return
        if child:
            current = await self._subagent_dao.get(parent_id, child.definition.subagent_id)
            receiver = self._sessions.get(event.session_id)
            closed = isinstance(receiver, SubagentSession) and not receiver.accepts_notifications()
            if current is None or ((current.retired_at is not None or closed) and event.kind not in {
                    InteractionEventKind.AUTHORIZATION_RESOLVED, InteractionEventKind.ERROR,
                    InteractionEventKind.HISTORY_RESTORED}):
                if event.kind == InteractionEventKind.AUTHORIZATION_RESOLVED:
                    self._approval_queue.remove(parent_id, event.session_id, event.request_id)
                    self._show_next_approval(parent_id)
                return
        if event.kind == InteractionEventKind.INTERRUPT:
            data = event.payload.get("value") or {}
            if data.get("type") == InterruptKind.PARENT_MESSAGE:
                self._parent(parent_id).notify_inbox_changed()
            elif event.request_id:
                self._approval_queue.enqueue(parent_id, event.session_id, event.request_id)
                if child:
                    self._show_next_approval(parent_id)
                elif self._approval_queue.head(parent_id) == (event.session_id, event.request_id):
                    self._publish(event)
            return
        if event.kind == InteractionEventKind.AUTHORIZATION_RESOLVED:
            self._approval_queue.remove(parent_id, event.session_id, event.request_id)
            if child:
                event = replace(event, session_id=parent_id, payload={**event.payload, "subagent_id": child.definition.subagent_id})
            self._publish(event)
            self._show_next_approval(parent_id)
        elif child:
            # 子的故障和恢复沿父订阅展示，并明确标识实际失败上下文。
            if event.kind in {InteractionEventKind.ERROR, InteractionEventKind.HISTORY_RESTORED}:
                self._publish(replace(event, session_id=parent_id,
                    payload={**event.payload, "subagent_id": child.definition.subagent_id}))
        else:
            self._publish(event)

    async def snapshot_subagents(self, session_id: str) -> tuple[SubagentSummary, ...]:
        """查询当前父归属内可用子的公开摘要。

        Args:
            session_id (str): 父配置编号。
        """
        if not self._settings_dao.exists(session_id):
            return ()
        return tuple(self._child(record.definition).summary() for record in await self._subagent_dao.list_available(session_id))

    async def get_subagent_details(self, session_id: str, subagent_id: str, interaction_limit: int = 10) -> dict[str, object]:
        """查询持久详情并合并该上下文自己提供的实时摘要。

        Args:
            session_id (str): 父配置编号。
            subagent_id (str): 实际子编号。
            interaction_limit (int): 最多返回的交互条数。
        """
        result = await self._subagents.get_details(session_id, subagent_id, interaction_limit)
        record = await self._subagent_dao.get(session_id, subagent_id)
        assert record is not None
        result["status"] = self._child(record.definition).summary().status if result["context_available"] else None
        return result

    def _check_source(self, source: SubagentSource, *, child: bool = False) -> None:
        """核验显式 Agent 请求的原轮次和身份，普通任务生产端保持独立。

        Args:
            source (SubagentSource): 工具绑定的来源。
            child (bool): 当前请求是否来自子向父发送消息。
        """
        if self.phase != RuntimePhase.RUNNING or not self._settings_dao.exists(source.session_id):
            raise RuntimeError("运行时关闭或父配置已删除")
        if child != (source.subagent_id is not None):
            raise ValueError("父子调用来源不匹配")
        session = self._sessions.get(source.subagent_id or source.session_id)
        if session is None or not session.matches_run(source.run_id):
            raise ValueError("调用来源执行已结束")
        if child:
            assert source.subagent_id is not None
            record = self._subagent_records.get(source.subagent_id)
            if record is None or record.definition.session_id != source.session_id:
                raise ValueError("父子归属不匹配")

    async def _accept_subagent_request(self, request: SubagentRequest):
        """在应用受理顺序中完成具名子操作，回执与随后执行分开交付。

        Args:
            request (SubagentRequest): 队列携带的子命令或只读查询。
        """
        if self.phase != RuntimePhase.RUNNING:
            raise RuntimeError("运行时正在关闭")
        if isinstance(request, SubagentSnapshotRequest):
            return await self.snapshot_subagents(request.session_id)
        async with self._mutation_coordinator.admission_lock:
            self._check_source(request.source, child=isinstance(request, ParentMessageRequest))
            if isinstance(request, CreateSubagentRequest):
                definition, created = await self._subagents.create(request)
                self._subagent_records[definition.subagent_id] = SubagentRecord(definition)
                return await self._child(definition).activate_created(created=created)
            if isinstance(request, InteractSubagentRequest):
                return await self._interact_subagent(request.source.session_id, request.subagent_id,
                    request.action, request.text, source_id=f"{request.source.run_id}:{request.source.tool_call_id}")
            if isinstance(request, SubagentDetailRequest):
                return await self.get_subagent_details(request.source.session_id, request.subagent_id, request.interaction_limit)
            assert request.source.subagent_id is not None
            record = await self._subagent_dao.get(request.source.session_id, request.source.subagent_id)
            assert record is not None
            await self._identity.require_available(request.source.session_id, request.source.subagent_id)
            _, created, _ = await self._subagent_dao.append_message(record.definition, SubagentInteractionKind.A2A,
                request.text, request.request_id, request_id=request.request_id)
            if created:
                self._parent(request.source.session_id).notify_inbox_changed()
            return None

    async def _interact_subagent(self, session_id: str, subagent_id: str, action: SubagentAction,
                                 text: str | None = None, *, source_id: str | None = None) -> CommandReceipt:
        """确认归属后调用子的完整公开操作。

        Args:
            session_id (str): 所属父编号。
            subagent_id (str): 实际子编号。
            action (SubagentAction): 消息、停止或销毁。
            text (str | None): 明确父消息正文。
            source_id (str | None): 固定调用来源，界面调用由本入口准备。
        """
        record = await self._subagent_dao.get(session_id, subagent_id)
        if record is None:
            return CommandReceipt(CommandStatus.NOT_FOUND, subagent_id)
        if record.retired_at is not None:
            return CommandReceipt(CommandStatus.ACCEPTED if action == SubagentAction.DESTROY else CommandStatus.CLOSED, subagent_id)
        self._subagent_records[subagent_id] = record
        session = self._child(record.definition)
        if action == SubagentAction.COMMAND:
            if text is None:
                return CommandReceipt(CommandStatus.INVALID, subagent_id, detail="消息正文不能为空")
            return await session.accept_message(text, source_id or uuid4().hex)
        if action == SubagentAction.STOP:
            return await session.stop_current()
        return await session.destroy()

    async def interact_subagent(self, session_id: str, subagent_id: str, action: SubagentAction,
                                text: str | None = None, *, source_id: str | None = None) -> CommandReceipt:
        """外部子管理入口在应用受理顺序中完成持久操作。

        Args:
            session_id (str): 父配置编号。
            subagent_id (str): 实际子编号。
            action (SubagentAction): 消息、停止或销毁。
            text (str | None): 父消息正文。
            source_id (str | None): 调用来源编号。
        """
        async with self._lifecycle_lock, self._mutation_coordinator.admission_lock:
            if self.phase != RuntimePhase.RUNNING or not self._settings_dao.exists(session_id):
                return CommandReceipt(CommandStatus.CLOSED, subagent_id)
            return await self._interact_subagent(session_id, subagent_id, SubagentAction(action), text, source_id=source_id)

    async def submit(self, command: ChannelCommand) -> CommandReceipt:
        """核验应用和渠道后调用会话公开操作，调用方保持会话锁为私有细节。

        Args:
            command (ChannelCommand): 渠道归一化的用户动作。

        Returns:
            CommandReceipt: 命令规定操作的真实受理结果。
        """
        async with self._lifecycle_lock, self._mutation_coordinator.admission_lock:
            if self.phase != RuntimePhase.RUNNING or not self._settings_dao.exists(command.session_id):
                return CommandReceipt(CommandStatus.CLOSED, command.session_id, detail="运行时关闭或父配置已删除")
            try:
                self.subscriptions.validate_source(command.origin_channel)
                if not await self.subscriptions.is_enabled(command.session_id, command.origin_channel):
                    raise ValueError("该会话未订阅此渠道")
                if command.kind == CommandKind.INTERACT_SUBAGENT:
                    if not command.subagent_id:
                        return CommandReceipt(CommandStatus.INVALID, command.session_id, detail="缺少子编号")
                    return await self._interact_subagent(command.session_id, command.subagent_id,
                        SubagentAction(command.payload.get("action", SubagentAction.STOP)), command.payload.get("text"))
                if command.kind == CommandKind.STOP_BACKGROUND_TASK:
                    record = await self.task_supervisor.get_task_snapshot(command.task_id) if command.task_id else None
                    if record is None or record.session_id != command.session_id:
                        return CommandReceipt(CommandStatus.NOT_FOUND, command.session_id)
                    return await self.task_supervisor.stop_task(record.task_id)
                if command.kind == CommandKind.RESOLVE_AUTHORIZATION and command.subagent_id:
                    record = await self._subagent_dao.get(command.session_id, command.subagent_id)
                    if record is None or record.retired_at is not None:
                        return CommandReceipt(CommandStatus.CLOSED, command.subagent_id)
                    return await self._child(record.definition).resolve_authorization(replace(command, session_id=command.subagent_id))
                session = self._parent(command.session_id)
                if command.kind == CommandKind.STOP_TURN:
                    return await session.stop_current(command.target_run_id)
                if command.kind == CommandKind.RESOLVE_AUTHORIZATION:
                    return await session.resolve_authorization(command)
                if command.kind == CommandKind.CANCEL_QUEUED:
                    return await session.cancel_queued(command)
                if command.kind in {CommandKind.HUMAN_TURN, CommandKind.HUMAN_STEER, CommandKind.QUEUE_TURN}:
                    return await session.accept_human(command)
                return CommandReceipt(CommandStatus.INVALID, command.session_id, detail="未知运行命令")
            except (ValueError, RuntimeError) as exc:
                return CommandReceipt(CommandStatus.FAILED, command.session_id, detail=str(exc))

    async def _handle_task_record(self, record: TaskRecord) -> None:
        """展示已保存任务事实，消费者只提醒仍可用的接收会话。

        Args:
            record (TaskRecord): 持久任务结果或当前进程的保存失败摘要。
        """
        if not self._settings_dao.exists(record.session_id):
            return
        if record.subagent_id and record.kind != TaskRecordKind.SUBAGENT:
            child = await self._subagent_dao.get(record.session_id, record.subagent_id)
            if child is None or child.retired_at is not None:
                return
            receiver = self._sessions.get(record.subagent_id)
            if isinstance(receiver, SubagentSession) and not receiver.accepts_notifications():
                return
        if record.background:
            self._publish(InteractionEvent(record.session_id, InteractionEventKind.TASK_STATUS,
                {"task_id": record.task_id, "status": record.status, "exit_code": record.exit_code,
                 "tool_name": record.tool_name, "error": record.error, "subagent_id": record.subagent_id}, task_id=record.task_id))
        if self.phase == RuntimePhase.RUNNING and record.status not in {TaskStatus.RUNNING, TaskExecutionPhase.SAVE_FAILED} and record.notify_agent:
            target = record.notification_context_id or record.session_id
            session = self._sessions.get(target)
            if session is None and record.notification_scope == AgentStorageScope.MAIN:
                session = self._parent(target)
            if session is not None:
                session.notify_inbox_changed()

    async def _receive_runtime_requests(self) -> None:
        """消费队列，展示事件和业务请求在同一位置处理，单项失败保持消费者可用。"""
        samples: list[SnapshotRequest] = []
        while True:
            request = await self.runtime_queue.get()
            try:
                if request is None:
                    for sample in samples:
                        if not sample.reply.done():
                            sample.reply.set_exception(RuntimeError("运行时已关闭"))
                    return
                if isinstance(request, SnapshotRequest):
                    samples.append(request)
                elif isinstance(request, ApprovalInvalidated):
                    self._remove_approval(request.context_id)
                elif isinstance(request, SessionClosed):
                    session = self._sessions.get(request.context_id)
                    if session is not None and id(session) == request.instance_id:
                        self._remove_approval(request.context_id)
                        self._sessions.pop(request.context_id)
                        self._subagent_records.pop(request.context_id, None)
                elif isinstance(request, (ExecutionEvent, InteractionEvent)):
                    await self._route_event(request)
                elif isinstance(request, TaskRecord):
                    await self._handle_task_record(request)
                elif isinstance(request, TaskStopRequest):
                    record = await self.task_supervisor.get_task_snapshot(request.task_id)
                    if record and record.session_id == request.session_id and (request.subagent_id is None or record.subagent_id == request.subagent_id):
                        await self.task_supervisor.stop_task(request.task_id)
                elif isinstance(request, (CreateSubagentRequest, InteractSubagentRequest, SubagentDetailRequest, SubagentSnapshotRequest, ParentMessageRequest)):
                    try:
                        async with self._lifecycle_lock:
                            value = await self._accept_subagent_request(request)
                        if not request.reply.done():
                            if isinstance(request, (CreateSubagentRequest, InteractSubagentRequest)):
                                request.reply.set_result(cast(CommandReceipt, value))
                            elif isinstance(request, SubagentDetailRequest):
                                request.reply.set_result(cast(dict[str, object], value))
                            elif isinstance(request, SubagentSnapshotRequest):
                                request.reply.set_result(cast(tuple[SubagentSummary, ...], value))
                            else:
                                request.reply.set_result(None)
                    except Exception as exc:
                        if not request.reply.done():
                            request.reply.set_exception(exc)
                else:
                    await self._accept_plugin_request(request)
                # 在队列已处理的位置同步复制状态和水位，数据库等待期间继续发布增量。
                if samples and self.runtime_queue.empty():
                    pending, samples = samples, []
                    for sample in pending:
                        if not sample.reply.done():
                            try:
                                payload = self._parent(sample.context_id).snapshot()
                                payload["approval_prompt"] = self._approval_prompt(sample.context_id)
                                epoch, sequence = self._watermark(sample.context_id)
                                sample.reply.set_result((payload, epoch, sequence))
                            except Exception as exc:
                                sample.reply.set_exception(exc)
            except Exception:
                logger.exception("运行请求处理失败：%s", request)
            finally:
                self.runtime_queue.task_done()

    def _remove_approval(self, context_id: str) -> None:
        """执行故障或会话结束后移除其展示请求，并展示下一项有效审批。

        Args:
            context_id (str): 审批已经失效的实际上下文。
        """
        child = self._subagent_records.get(context_id)
        parent_id = child.definition.session_id if child else context_id
        self._approval_queue.remove(parent_id, context_id)
        if self._settings_dao.exists(parent_id):
            self._show_next_approval(parent_id)

    async def _accept_plugin_request(self, request: PluginRequest) -> None:
        """显式插件请求经过应用入口核验，通知政策由父会话决定。

        Args:
            request (PluginRequest): 已绑定插件和轮次来源的请求。
        """
        async with self._lifecycle_lock, self._mutation_coordinator.admission_lock:
            if self.phase != RuntimePhase.RUNNING or not self._settings_dao.exists(request.session_id):
                return
            if request.kind == PluginRequestKind.STOP_TASK:
                if not request.task_id:
                    return
                record = await self.task_supervisor.get_task_snapshot(request.task_id)
                if record and record.session_id == request.session_id and record.plugin_name == request.plugin_name:
                    await self.task_supervisor.stop_task(record.task_id)
                return
            await self._parent(request.session_id).accept_plugin(request)

    async def snapshot(self, session_id: str) -> InteractionEvent:
        """先取得对应的运行摘要和输出水位，再查询持久输入和任务。

        Args:
            session_id (str): 需要重连的父会话。

        Returns:
            InteractionEvent: 查询期间产生的后续事件仍由订阅缓冲保留。
        """
        if self._task_reader is None or self._task_reader.done():
            payload = self._parent(session_id).snapshot()
            epoch, sequence = self._watermark(session_id)
        else:
            reply = asyncio.get_running_loop().create_future()
            self.runtime_queue.put_nowait(SnapshotRequest(session_id, reply))
            payload, epoch, sequence = await reply
        manager = self._manager_factory(session_id)
        try:
            pending = await manager.pending_humans()
        finally:
            await manager.close()
        payload["pending_inputs"] = [{"input_sequence": item.id, "origin_channel": item.origin_channel,
            "delivery_mode": item.delivery_mode} for item in pending]
        payload["queued_inputs"] = [{"input_sequence": item.id, "message": item.payload["message"],
            "images": item.payload.get("images") or [], "origin_channel": item.origin_channel}
            for item in pending if item.delivery_mode == InboxDeliveryMode.QUEUED]
        payload["tasks"] = [asdict(item) for item in await self.task_supervisor.list_tasks(session_id)]
        payload["subagents"] = [asdict(item) for item in await self.snapshot_subagents(session_id)]
        return InteractionEvent(session_id, InteractionEventKind.SESSION_SNAPSHOT, payload,
                                stream_epoch=epoch, event_sequence=sequence)

    async def _reset_inbox(self) -> None:
        """启动时清除旧真人意图，保留通知，已删除父的输入全部作废。"""
        manager = self._manager_factory("__runtime_init__")
        try:
            await manager.reset_after_restart()
            pending = await manager.pending_sessions()
        finally:
            await manager.close()
        for identifier in pending:
            if not self._settings_dao.exists(identifier):
                manager = self._manager_factory(identifier)
                try:
                    await manager.discard_pending_inputs()
                finally:
                    await manager.close()

    async def _finalize_abandoned_sessions(self) -> None:
        """启动时补齐上次进程的协议，保持模型、工具和等待均不自动恢复。"""
        manager = self._manager_factory("__runtime_init__")
        try:
            identifiers = await manager.unfinished_sessions()
        finally:
            await manager.close()
        for identifier in identifiers:
            manager = self._manager_factory(identifier)
            try:
                await manager.finalize_abandoned_turn()
            finally:
                await manager.close()

    async def start(self) -> None:
        """整理成功后才开放入口，启动失败结束本实例并交回应用清理资源。"""
        async with self._lifecycle_lock:
            if self.phase == RuntimePhase.RUNNING:
                return
            if self.phase != RuntimePhase.INITIALIZING:
                raise RuntimeError("运行时实例只能启动一次")
            try:
                await self._reset_inbox()
                await self._finalize_abandoned_sessions()
                available = await self._subagents.recover()
                self._subagent_records = {item.definition.subagent_id: item for item in available}
            except BaseException:
                self.phase = RuntimePhase.STOPPED
                raise
            self.phase = RuntimePhase.RUNNING
            self._task_reader = asyncio.create_task(self._receive_runtime_requests())

    def schedule_parent_cleanup(self, session_id: str) -> None:
        """父配置删除已经提交后封闭入口并安排一次后台清理。

        Args:
            session_id (str): 已经删除的父配置身份。
        """
        if self._settings_dao.exists(session_id):
            raise ValueError("父配置仍存在")
        for session in self._sessions.values():
            if session.context_id == session_id or (
                    isinstance(session, SubagentSession) and session.definition.session_id == session_id):
                session.begin_close()
        if session_id not in self._parent_cleanup_workers:
            self._parent_cleanup_workers[session_id] = asyncio.create_task(self._cleanup_parent(session_id))

    async def _cleanup_parent(self, session_id: str) -> None:
        """组织所属子废弃和任务停止，实际资源在各自 close 内有限处理。

        Args:
            session_id (str): 已删除父身份。
        """
        try:
            operations = [self._retire_children(session_id), self.task_supervisor.stop_session_tasks(session_id)]
            operations.extend(session.close("父配置删除") for session in self._sessions.values()
                if session.context_id == session_id or (
                    isinstance(session, SubagentSession) and session.definition.session_id == session_id))
            results = await asyncio.gather(*operations, return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException):
                    logger.error("父删除后关闭所属资源失败：%s，%s", session_id, result)
            manager = self._manager_factory(session_id)
            try:
                await manager.discard_pending_inputs()
            finally:
                await manager.close()
            if self._cleanup_session_connections:
                await self._cleanup_session_connections(session_id)
        except Exception:
            logger.exception("父配置删除后的资源收尾失败：%s", session_id)
        finally:
            self._parent_cleanup_workers.pop(session_id, None)

    async def _retire_children(self, session_id: str) -> None:
        """废弃已删除父的持久子身份，运行对象由注册表安排关闭。

        Args:
            session_id (str): 已删除的父配置编号。
        """
        records = await self._subagent_dao.list_records(session_id)
        for record in records:
            identifier = record.definition.subagent_id
            await self._subagent_dao.retire(identifier)
            if identifier not in self._sessions:
                self._subagent_records.pop(identifier, None)

    async def begin_shutdown(self) -> None:
        """封闭应用入口并通知各会话结束，等待已经开始的事务确认。"""
        async with self._lifecycle_lock:
            if self.phase in {RuntimePhase.STOPPING, RuntimePhase.STOPPED}:
                return
            self.phase = RuntimePhase.STOPPING
            for session in self._sessions.values():
                session.begin_close()

    async def wait_for_workers(self) -> None:
        """等待所有会话和父删除收尾，运行消费者在生产者结束前继续工作。"""
        results = await asyncio.gather(*(session.close("应用退出") for session in self._sessions.values()),
            *self._parent_cleanup_workers.values(), return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                logger.error("会话关闭失败：%s", result)

    async def close(self) -> None:
        """生产者结束后排空队列并结束消费者，共享存储和 Hub 由应用关闭。"""
        await self.begin_shutdown()
        await self.wait_for_workers()
        if self._task_reader is not None and not self._task_reader.done():
            await self.runtime_queue.join()
            self.runtime_queue.put_nowait(None)
            await self._task_reader
        self._task_reader = None
        self.phase = RuntimePhase.STOPPED
