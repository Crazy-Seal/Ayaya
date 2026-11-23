"""处理用户操作、Agent 对话管理、运行时请求和界面事件发布。"""

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
from app.interaction.interaction_contract import InteractionCommand, InteractionEvent
from app.interaction.runtime_events import to_interaction_event
from app.schemas.agent_storage import AgentStorageScope
from app.schemas.runtime_types import CommandKind, CommandStatus, InboxDeliveryMode, InteractionEventKind, InterruptKind, RuntimePhase, TaskExecutionPhase, TaskStatus
from app.schemas.subagent_types import SubagentAction, SubagentDefinition, SubagentInteractionKind, SubagentRecord, SubagentSummary, TaskRecordKind
from app.schemas.task_types import PluginRequest, PluginRequestKind, TaskRecord, TaskStopRequest
from app.services.agent_identity_service import AgentIdentityService
from app.services.settings_mutation import SettingsMutationCoordinator
from app.services.subagent_service import SubagentService


logger = logging.getLogger(__name__)


class AgentRuntimeScheduler:
    """管理后端运行期间的用户操作、Agent 对话和界面更新。

    用户消息、审批决定和子 Agent 操作交给相应的 ParentSession 或 SubagentSession 处理。
    Agent 和后台任务产生的消息与状态通过界面事件通知用户。

    Attributes:
        phase (RuntimePhase): 后端运行阶段，用于决定能否受理新的操作。
        runtime_queue (asyncio.Queue[RuntimeRequest]): 接收后端各组件请求和事件的运行时请求队列。
        task_supervisor (TaskSupervisor): 提供后台任务启动、停止和查询功能。
        _sessions (dict[str, ParentSession | SubagentSession]): 按 Agent 对话编号查找对应的会话对象。
        _subagent_records (dict[str, SubagentRecord]): 已加载的子 Agent 记录，用于查询子 Agent 所属的主会话。
        _task_reader (asyncio.Task[None] | None): 运行时请求队列处理循环。
        _approval_queue (ApprovalQueue): 为各主会话安排用户审批请求的展示顺序。
        _parent_cleanup_workers (dict[str, asyncio.Task[None]]): 主会话配置删除后正在进行的资源清理工作。
    """

    def __init__(self, *, runtime_queue: asyncio.Queue[RuntimeRequest],
                 parent_factory: Callable[[str], ParentSession],
                 child_factory: Callable[[SubagentDefinition], SubagentSession],
                 manager_factory: Callable[[str], SessionStateManager],
                 identity: AgentIdentityService, subagents: SubagentService,
                 task_supervisor: TaskSupervisor, mutation_coordinator: SettingsMutationCoordinator,
                 publish: Callable[[InteractionEvent], InteractionEvent],
                 watermark: Callable[[str], tuple[str, int]],
                 cleanup_session_connections: Callable[[str], Awaitable[None]] | None = None) -> None:
        """创建 AgentRuntimeScheduler，供后端接收用户操作、管理 Agent 对话和发布界面事件。

        Args:
            runtime_queue (asyncio.Queue[RuntimeRequest]): 后端各组件发送请求和事件的运行时请求队列。
            parent_factory (Callable[[str], ParentSession]): 根据主会话编号提供 ParentSession。
            child_factory (Callable[[SubagentDefinition], SubagentSession]): 根据子 Agent 定义提供 SubagentSession。
            manager_factory (Callable[[str], SessionStateManager]): 为指定主会话提供加载和保存 Agent 状态的服务。
            identity (AgentIdentityService): 检查主会话配置和子 Agent 是否仍可使用。
            subagents (SubagentService): 创建子 Agent、查询详情和保存委派任务结果的服务。
            task_supervisor (TaskSupervisor): 启动、停止和查询后台任务的服务。
            mutation_coordinator (SettingsMutationCoordinator): 协调会话配置删除和新请求受理的先后关系。
            publish (Callable[[InteractionEvent], InteractionEvent]): 向界面发布事件并返回带编号的界面事件。
            watermark (Callable[[str], tuple[str, int]]): 查询指定主会话的应用启动批次编号和最晚已发布事件序号。
            cleanup_session_connections (Callable[[str], Awaitable[None]] | None): 主会话配置删除后释放其共享连接的操作。
        """
        self.runtime_queue, self.task_supervisor = runtime_queue, task_supervisor
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
        """为用户操作或状态查询取得指定主会话的 ParentSession。

        Args:
            context_id (str): 主会话编号。

        Returns:
            ParentSession: 该主会话对应的 ParentSession，首次使用时创建。

        Raises:
            ValueError: 该编号已用于子 Agent 对话。
        """
        session = self._sessions.get(context_id)
        if session is None:
            session = self._parent_factory(context_id)
            self._sessions[context_id] = session
        if not isinstance(session, ParentSession):
            raise ValueError("目标是子上下文")
        return session

    def _child(self, definition: SubagentDefinition) -> SubagentSession:
        """为子 Agent 操作或状态查询取得对应的 SubagentSession。

        Args:
            definition (SubagentDefinition): 目标子 Agent 的定义，包含其编号和所属主会话。

        Returns:
            SubagentSession: 该子 Agent 对话对应的 SubagentSession，首次使用时创建。

        Raises:
            ValueError: 子 Agent 编号已用于主会话。
        """
        session = self._sessions.get(definition.subagent_id)
        if session is None:
            session = self._child_factory(definition)
            self._sessions[definition.subagent_id] = session
        if not isinstance(session, SubagentSession):
            raise ValueError("目标是父上下文")
        return session

    def _approval_prompt(self, session_id: str) -> dict[str, object] | None:
        """查询指定主会话当前应向用户展示的审批请求。

        Args:
            session_id (str): 审批请求所属的主会话编号。

        Returns:
            dict[str, object] | None: 审批内容、审批请求编号和可选的子 Agent 编号。没有可展示审批时为空。
        """
        entry = self._approval_queue.head(session_id)
        if entry is None:
            return None
        session = self._sessions.get(entry[0]) if entry else None
        value = session.snapshot()["interrupt"] if session else None
        return {"value": value, "subagent_id": entry[0] if entry[0] != session_id else None,
                "request_id": entry[1]} if value else None

    def _show_next_approval(self, session_id: str) -> None:
        """通知界面显示指定主会话当前排在首位的审批请求。

        Args:
            session_id (str): 需要展示审批的主会话编号。
        """
        prompt = self._approval_prompt(session_id)
        if prompt:
            kind = InteractionEventKind.SUBAGENT_INTERRUPT if prompt["subagent_id"] else InteractionEventKind.INTERRUPT
            self._publish(InteractionEvent(session_id, kind, prompt, request_id=str(prompt["request_id"])))

    async def _route_event(self, value: ExecutionEvent | InteractionEvent) -> None:
        """处理 Agent 执行期间事件和界面事件，发布界面更新并处理审批展示。

        子 Agent 等待主 Agent 回复时，提醒主会话检查 SessionInbox。

        Args:
            value (ExecutionEvent | InteractionEvent): Agent 执行期间产生的事件，或会话已准备的界面事件。
        """
        # 1. 确认事件所属的主会话仍然存在。
        event = to_interaction_event(value)
        child = self._subagent_records.get(event.session_id)
        parent_id = child.definition.session_id if child else event.session_id
        if not self._settings_dao.exists(parent_id):
            return
        # 1.1. 子 Agent 关闭或废弃后，继续向界面提供审批结束、错误和恢复已保存状态的通知。
        if child:
            current = await self._subagent_dao.get(parent_id, child.definition.subagent_id)
            receiver = self._sessions.get(event.session_id)
            closed = isinstance(receiver, SubagentSession) and not receiver.accepts_notifications()
            if current is None or ((current.retired_at is not None or closed) and event.kind not in {
                    InteractionEventKind.AUTHORIZATION_RESOLVED, InteractionEventKind.ERROR,
                    InteractionEventKind.HISTORY_RESTORED}):
                # 1.2. 子 Agent 记录已删除时，移除已结束的审批并展示下一项。
                if event.kind == InteractionEventKind.AUTHORIZATION_RESOLVED:
                    self._approval_queue.remove(parent_id, event.session_id, event.request_id)
                    self._show_next_approval(parent_id)
                return
        # 2. 区分等待主 Agent 回复和等待用户审批，分别通知主会话或界面。
        if event.kind == InteractionEventKind.INTERRUPT:
            data = event.payload.get("value") or {}
            # 2.1. 子 Agent 的消息已保存到主会话的 SessionInbox，提醒主会话处理。
            if data.get("type") == InterruptKind.WAIT_FOR_PARENT:
                self._parent(parent_id).notify_inbox_changed()
            # 2.2. 同一主会话的用户审批按先后顺序展示。
            elif event.request_id:
                self._approval_queue.enqueue(parent_id, event.session_id, event.request_id)
                if child:
                    self._show_next_approval(parent_id)
                elif self._approval_queue.head(parent_id) == (event.session_id, event.request_id):
                    self._publish(event)
            return
        # 3. 审批决定已保存或审批已取消时，通知界面并展示下一项。
        if event.kind == InteractionEventKind.AUTHORIZATION_RESOLVED:
            self._approval_queue.remove(parent_id, event.session_id, event.request_id)
            if child:
                event = replace(event, session_id=parent_id, payload={**event.payload, "subagent_id": child.definition.subagent_id})
            self._publish(event)
            self._show_next_approval(parent_id)
        elif child:
            # 4. 子 Agent 的错误和恢复通知通过所属主会话发布，并附上子 Agent 编号。
            if event.kind in {InteractionEventKind.ERROR, InteractionEventKind.HISTORY_RESTORED}:
                self._publish(replace(event, session_id=parent_id,
                    payload={**event.payload, "subagent_id": child.definition.subagent_id}))
        else:
            # 5. 将其他属于主会话的事件发布到界面。
            self._publish(event)

    async def snapshot_subagents(self, session_id: str) -> tuple[SubagentSummary, ...]:
        """查询指定主会话下仍可使用的子 Agent 及其当前状态。

        子 Agent 列表查询、会话状态快照查询和主 Agent 请求模型前更新子 Agent 名单时使用。

        Args:
            session_id (str): 子 Agent 所属的主会话编号。

        Returns:
            tuple[SubagentSummary, ...]: 子 Agent 状态摘要。主会话配置不存在时返回空元组。
        """
        if not self._settings_dao.exists(session_id):
            return ()
        return tuple(self._child(record.definition).summary() for record in await self._subagent_dao.list_available(session_id))

    async def get_subagent_details(self, session_id: str, subagent_id: str, interaction_limit: int = 10) -> dict[str, object]:
        """提供子 Agent 的定义、当前状态、最近交互和委派任务结果。

        用户查看子 Agent 详情，或主 Agent 调用详情查询工具时使用。

        Args:
            session_id (str): 子 Agent 所属的主会话编号。
            subagent_id (str): 需要查询的子 Agent 编号。
            interaction_limit (int): 最多返回的最近交互条数。

        Returns:
            dict[str, object]: 子 Agent 详情。已废弃的子 Agent 的当前状态为空。

        Raises:
            ValueError: 最近交互条数小于 1。
            ResourceNotFoundError: 子 Agent 不存在，或不属于指定主会话。
        """
        result = await self._subagents.get_details(session_id, subagent_id, interaction_limit)
        record = await self._subagent_dao.get(session_id, subagent_id)
        assert record is not None
        result["status"] = self._child(record.definition).summary().status if result["context_available"] else None
        return result

    def _check_source(self, source: SubagentSource, *, child: bool = False) -> None:
        """检查子 Agent 管理请求和子 Agent 消息是否来自有效的执行轮次。

        Args:
            source (SubagentSource): 发出请求的主会话、子 Agent、执行轮次和工具调用信息。
            child (bool): 是否要求请求来自向主 Agent 发消息的子 Agent。

        Raises:
            RuntimeError: 后端尚未运行、正在关闭，或主会话配置已删除。
            ValueError: 请求来源的主 Agent 与子 Agent 方向、执行轮次或所属主会话不匹配。
        """
        # 1. 确认后端和请求所属的主会话仍可接受操作。
        if self.phase != RuntimePhase.RUNNING or not self._settings_dao.exists(source.session_id):
            raise RuntimeError("运行时关闭或父配置已删除")
        # 2. 核对请求应来自主 Agent 还是子 Agent。
        if child != (source.subagent_id is not None):
            raise ValueError("父子调用来源不匹配")
        # 3. 确认发出工具请求的执行轮次仍然有效。
        session = self._sessions.get(source.subagent_id or source.session_id)
        if session is None or not session.matches_run(source.run_id):
            raise ValueError("调用来源执行已结束")
        # 4. 子 Agent 向主 Agent 发消息时，检查双方所属的主会话一致。
        if child:
            assert source.subagent_id is not None
            record = self._subagent_records.get(source.subagent_id)
            if record is None or record.definition.session_id != source.session_id:
                raise ValueError("父子归属不匹配")

    async def _accept_subagent_request(self, request: SubagentRequest):
        """响应创建和操作子 Agent、查询子 Agent 信息或向主 Agent 发消息的请求。

        Args:
            request (SubagentRequest): 工具或模型请求前的名单查询发到运行时请求队列的请求。

        Returns:
            CommandReceipt | dict[str, object] | tuple[SubagentSummary, ...] | None:
                创建和操作子 Agent 返回命令受理结果，查询返回详情或状态摘要，向主 Agent 发消息返回空值。

        Raises:
            RuntimeError: 后端正在关闭，或请求所属的 Agent 已无法使用。
            ValueError: 请求的调用来源或操作参数无效。
        """
        if self.phase != RuntimePhase.RUNNING:
            raise RuntimeError("运行时正在关闭")
        # 1. 主 Agent 请求模型前，提供当前子 Agent 名单。
        if isinstance(request, SubagentSnapshotRequest):
            return await self.snapshot_subagents(request.session_id)
        # 2. 检查工具请求的调用来源，再处理对应的子 Agent 操作。
        async with self._mutation_coordinator.admission_lock:
            self._check_source(request.source, child=isinstance(request, ParentMessageRequest))
            # 2.1. 保存新子 Agent 和初始指令后，安排其开始委派任务。
            if isinstance(request, CreateSubagentRequest):
                definition, created = await self._subagents.create(request)
                self._subagent_records[definition.subagent_id] = SubagentRecord(definition)
                return await self._child(definition).activate_created(created=created)
            # 2.2. 处理向子 Agent 发消息、停止委派任务和销毁子 Agent 的请求。
            if isinstance(request, InteractSubagentRequest):
                return await self._interact_subagent(request.source.session_id, request.subagent_id,
                    request.action, request.text, source_id=f"{request.source.run_id}:{request.source.tool_call_id}")
            # 2.3. 查询指定子 Agent 的详情。
            if isinstance(request, SubagentDetailRequest):
                return await self.get_subagent_details(request.source.session_id, request.subagent_id, request.interaction_limit)
            # 2.4. 保存子 Agent 发给主 Agent 的消息，成功新增后提醒主会话处理。
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
        """向指定子 Agent 发送消息、停止其委派任务或销毁子 Agent。

        Args:
            session_id (str): 子 Agent 所属的主会话编号。
            subagent_id (str): 操作目标的子 Agent 编号。
            action (SubagentAction): 发送消息、停止委派任务或销毁子 Agent。
            text (str | None): 发给子 Agent 的消息正文。
            source_id (str | None): 消息的来源记录编号，用于识别重复提交的同一条消息。

        Returns:
            CommandReceipt: 消息保存、停止委派任务或销毁子 Agent 的受理结果。
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
        """供后端调用方直接向子 Agent 发送消息、停止委派任务或销毁子 Agent。

        Args:
            session_id (str): 子 Agent 所属的主会话编号。
            subagent_id (str): 操作目标的子 Agent 编号。
            action (SubagentAction): 发送消息、停止委派任务或销毁子 Agent。
            text (str | None): 发给子 Agent 的消息正文。
            source_id (str | None): 消息的来源记录编号，用于识别重复提交的同一条消息。

        Returns:
            CommandReceipt: 当前操作的受理结果。后端正在关闭或主会话配置已删除时返回 CLOSED。
        """
        async with self._lifecycle_lock, self._mutation_coordinator.admission_lock:
            if self.phase != RuntimePhase.RUNNING or not self._settings_dao.exists(session_id):
                return CommandReceipt(CommandStatus.CLOSED, subagent_id)
            return await self._interact_subagent(session_id, subagent_id, SubagentAction(action), text, source_id=source_id)

    async def submit(self, command: InteractionCommand) -> CommandReceipt:
        """处理用户发送消息、停止执行、提交审批、撤销排队和管理子 Agent 的操作。

        Args:
            command (InteractionCommand): 用户通过交互入口提交的操作，包含主会话编号及目标信息。

        Returns:
            CommandReceipt: 该操作的命令受理结果，包含受理状态、目标编号及失败说明。
        """
        # 1. 确认后端和主会话仍可受理此次用户操作。
        async with self._lifecycle_lock, self._mutation_coordinator.admission_lock:
            if self.phase != RuntimePhase.RUNNING or not self._settings_dao.exists(command.session_id):
                return CommandReceipt(CommandStatus.CLOSED, command.session_id, detail="运行时关闭或父配置已删除")
            # 2. 按用户操作选择目标子 Agent、后台任务或主会话。
            try:
                # 2.1. 处理向子 Agent 发消息、停止委派任务和销毁子 Agent 的操作。
                if command.kind == CommandKind.INTERACT_SUBAGENT:
                    if not command.subagent_id:
                        return CommandReceipt(CommandStatus.INVALID, command.session_id, detail="缺少子编号")
                    return await self._interact_subagent(command.session_id, command.subagent_id,
                        SubagentAction(command.payload.get("action", SubagentAction.STOP)), command.payload.get("text"))
                # 2.2. 确认后台任务属于当前主会话后，请求停止任务。
                if command.kind == CommandKind.STOP_BACKGROUND_TASK:
                    record = await self.task_supervisor.get_task_snapshot(command.task_id) if command.task_id else None
                    if record is None or record.session_id != command.session_id:
                        return CommandReceipt(CommandStatus.NOT_FOUND, command.session_id)
                    return await self.task_supervisor.stop_task(record.task_id)
                # 2.3. 将子 Agent 的审批决定交给对应的 SubagentSession。
                if command.kind == CommandKind.RESOLVE_AUTHORIZATION and command.subagent_id:
                    record = await self._subagent_dao.get(command.session_id, command.subagent_id)
                    if record is None or record.retired_at is not None:
                        return CommandReceipt(CommandStatus.CLOSED, command.subagent_id)
                    return await self._child(record.definition).resolve_authorization(replace(command, session_id=command.subagent_id))
                # 2.4. 将主会话的消息、停止、审批和撤销排队操作交给 ParentSession。
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
            # 3. 操作参数或运行状态出错时，返回失败的命令受理结果。
            except (ValueError, RuntimeError) as exc:
                return CommandReceipt(CommandStatus.FAILED, command.session_id, detail=str(exc))

    async def _handle_task_record(self, record: TaskRecord) -> None:
        """收到任务状态变化时更新界面，并提醒接收 Agent 处理已保存的任务结果通知。

        Args:
            record (TaskRecord): 任务开始、结束或结果保存失败时提供的任务信息。
        """
        if not self._settings_dao.exists(record.session_id):
            return
        # 1. 将后台任务状态显示到主会话界面，便于用户继续查看任务进展。
        if record.background:
            self._publish(InteractionEvent(record.session_id, InteractionEventKind.TASK_STATUS,
                {"task_id": record.task_id, "status": record.status, "exit_code": record.exit_code,
                 "tool_name": record.tool_name, "error": record.error, "subagent_id": record.subagent_id}, task_id=record.task_id))
        # 2. 任务结束且结果已保存时，提醒仍可接收通知的 Agent 处理任务结果。
        if self.phase == RuntimePhase.RUNNING and record.status not in {TaskStatus.RUNNING, TaskExecutionPhase.SAVE_FAILED} and record.notify_agent:
            # 2.1. 任务由子 Agent 创建时，确认该子 Agent 仍存在、尚未废弃且对话仍可接收通知。
            if record.subagent_id and record.kind != TaskRecordKind.SUBAGENT:
                child = await self._subagent_dao.get(record.session_id, record.subagent_id)
                if child is None or child.retired_at is not None:
                    return
                receiver = self._sessions.get(record.subagent_id)
                if isinstance(receiver, SubagentSession) and not receiver.accepts_notifications():
                    return
            # 2.2. 提醒接收结果的会话检查 SessionInbox。
            target = record.notification_context_id or record.session_id
            session = self._sessions.get(target)
            if session is None and record.notification_scope == AgentStorageScope.MAIN:
                session = self._parent(target)
            if session is not None:
                session.notify_inbox_changed()

    async def _receive_runtime_requests(self) -> None:
        """在后端运行期间处理工具请求、插件请求、任务通知和 Agent 执行期间事件。

        处理结果返回给等待的调用方，需要展示的消息和状态发送给界面。
        """
        samples: list[SnapshotRequest] = []
        while True:
            request = await self.runtime_queue.get()
            try:
                # 1. 应用要求结束处理循环时，结束仍在等待的会话状态快照查询。
                if request is None:
                    for sample in samples:
                        if not sample.reply.done():
                            sample.reply.set_exception(RuntimeError("运行时已关闭"))
                    return
                # 2. 根据请求类型处理查询、会话关闭、事件、任务和插件操作。
                if isinstance(request, SnapshotRequest):
                    # 2.1. 会话状态快照查询等待当前队列中的事件处理完成后再取值。
                    samples.append(request)
                # 2.2. Agent 保存故障使审批失效时，移除对应审批展示项。
                elif isinstance(request, ApprovalInvalidated):
                    self._remove_approval(request.context_id)
                # 2.3. 核对关闭通知对应的对象后，移除该会话及其审批展示项。
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
                # 2.4. 工具请求停止后台任务时，按主会话和子 Agent 归属确认目标。
                elif isinstance(request, TaskStopRequest):
                    record = await self.task_supervisor.get_task_snapshot(request.task_id)
                    if record and record.session_id == request.session_id and (request.subagent_id is None or record.subagent_id == request.subagent_id):
                        await self.task_supervisor.stop_task(request.task_id)
                # 2.5. 处理子 Agent 请求，将命令受理结果、查询结果或错误返回调用方。
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
                # 3. 当前队列中的事件处理完后，读取会话状态及对应的事件编号，供界面衔接后续事件。
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
            # 4. 记录本次请求的错误，后续请求继续处理。
            except Exception:
                logger.exception("运行请求处理失败：%s", request)
            finally:
                # 5. 标记当前请求处理结束，供应用退出时确认队列中的请求全部处理完成。
                self.runtime_queue.task_done()

    def _remove_approval(self, context_id: str) -> None:
        """Agent 保存故障或对话关闭后移除其审批展示项，并展示下一项审批。

        Args:
            context_id (str): 审批已失效的 Agent 对话编号。
        """
        child = self._subagent_records.get(context_id)
        parent_id = child.definition.session_id if child else context_id
        self._approval_queue.remove(parent_id, context_id)
        if self._settings_dao.exists(parent_id):
            self._show_next_approval(parent_id)

    async def _accept_plugin_request(self, request: PluginRequest) -> None:
        """响应插件发出的通知、执行控制、任务停止和界面提示请求。

        Args:
            request (PluginRequest): 插件提交的操作，包含插件名称、所属主会话和操作内容。
        """
        # 1. 确认后端和插件所属的主会话仍可接受操作。
        async with self._lifecycle_lock, self._mutation_coordinator.admission_lock:
            if self.phase != RuntimePhase.RUNNING or not self._settings_dao.exists(request.session_id):
                return
            # 2. 插件请求停止任务时，确认任务属于该主会话和该插件。
            if request.kind == PluginRequestKind.STOP_TASK:
                if not request.task_id:
                    return
                record = await self.task_supervisor.get_task_snapshot(request.task_id)
                if record and record.session_id == request.session_id and record.plugin_name == request.plugin_name:
                    await self.task_supervisor.stop_task(record.task_id)
                return
            # 3. 将插件通知、执行控制和界面提示请求交给 ParentSession。
            await self._parent(request.session_id).accept_plugin(request)

    async def snapshot(self, session_id: str) -> InteractionEvent:
        """为页面加载、会话切换或重连提供指定主会话的会话状态快照。

        Args:
            session_id (str): 页面需要展示的主会话编号。

        Returns:
            InteractionEvent: 会话状态、Agent 执行状态快照、审批提示、待处理输入、任务和子 Agent 信息。
                同时包含应用启动批次编号和该会话的最晚已发布事件序号。
        """
        # 1. 取得主会话当前状态及对应的应用启动批次编号和最晚已发布事件序号。
        if self._task_reader is None or self._task_reader.done():
            payload = self._parent(session_id).snapshot()
            epoch, sequence = self._watermark(session_id)
        else:
            reply = asyncio.get_running_loop().create_future()
            self.runtime_queue.put_nowait(SnapshotRequest(session_id, reply))
            payload, epoch, sequence = await reply
        # 2. 补充 SessionInbox 中的待处理用户消息，供界面恢复排队显示。
        manager = self._manager_factory(session_id)
        try:
            pending = await manager.pending_humans()
        finally:
            await manager.close()
        payload["pending_inputs"] = [{"input_sequence": item.id, "delivery_mode": item.delivery_mode} for item in pending]
        payload["queued_inputs"] = [{"input_sequence": item.id, "message": item.payload["message"],
            "images": item.payload.get("images") or []}
            for item in pending if item.delivery_mode == InboxDeliveryMode.QUEUED]
        # 3. 补充任务和子 Agent 状态，形成页面需要的会话状态快照。
        payload["tasks"] = [asdict(item) for item in await self.task_supervisor.list_tasks(session_id)]
        payload["subagents"] = [asdict(item) for item in await self.snapshot_subagents(session_id)]
        return InteractionEvent(session_id, InteractionEventKind.SESSION_SNAPSHOT, payload,
                                stream_epoch=epoch, event_sequence=sequence)

    async def _reset_inbox(self) -> None:
        """后端启动时清理上次启动留下的待处理用户消息和已删除主会话的待处理输入。"""
        # 1. 删除上次启动尚未交付的用户消息，保留待处理任务结果通知和插件通知。
        manager = self._manager_factory("__runtime_init__")
        try:
            await manager.reset_after_restart()
            pending = await manager.pending_sessions()
        finally:
            await manager.close()
        # 2. 主会话配置已删除时，清除其全部待处理输入。
        for identifier in pending:
            if not self._settings_dao.exists(identifier):
                manager = self._manager_factory(identifier)
                try:
                    await manager.discard_pending_inputs()
                finally:
                    await manager.close()

    async def _finalize_abandoned_sessions(self) -> None:
        """后端启动时为上次启动中断的主 Agent 执行轮次补充工具结果，并保存 COMPLETED 类型的 checkpoint。"""
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
        """后端启动时整理上次启动未完成的工作，并开始处理新的用户操作和运行时请求。

        Raises:
            RuntimeError: 当前实例正在关闭或已经停止，无法再次启动。
            Exception: 上次启动遗留工作的整理失败。
        """
        async with self._lifecycle_lock:
            if self.phase == RuntimePhase.RUNNING:
                return
            if self.phase != RuntimePhase.INITIALIZING:
                raise RuntimeError("运行时实例只能启动一次")
            # 1. 整理上次启动留下的待处理输入和未完成的主 Agent 执行轮次。
            try:
                await self._reset_inbox()
                await self._finalize_abandoned_sessions()
                # 1.1. 整理子 Agent 的遗留委派任务，取得仍可再次使用的子 Agent 记录。
                available = await self._subagents.recover()
                self._subagent_records = {item.definition.subagent_id: item for item in available}
            # 2. 启动整理失败时，标记本实例已停止并向应用报告错误。
            except BaseException:
                self.phase = RuntimePhase.STOPPED
                raise
            # 3. 开始受理新操作，并启动运行时请求队列处理循环。
            self.phase = RuntimePhase.RUNNING
            self._task_reader = asyncio.create_task(self._receive_runtime_requests())

    def schedule_parent_cleanup(self, session_id: str) -> None:
        """主会话配置删除成功后，安排停止所属 Agent 的工作并释放资源。

        Args:
            session_id (str): 已删除配置的主会话编号。

        Raises:
            ValueError: 指定主会话的配置仍然存在。
        """
        # 1. 确认主会话配置已经删除。
        if self._settings_dao.exists(session_id):
            raise ValueError("父配置仍存在")
        # 2. 让已加载的主会话和子 Agent 对话进入关闭状态。
        for session in self._sessions.values():
            if session.context_id == session_id or (
                    isinstance(session, SubagentSession) and session.definition.session_id == session_id):
                session.begin_close()
        # 3. 为该主会话安排资源清理，重复调用时沿用正在进行的清理工作。
        if session_id not in self._parent_cleanup_workers:
            self._parent_cleanup_workers[session_id] = asyncio.create_task(self._cleanup_parent(session_id))

    async def _cleanup_parent(self, session_id: str) -> None:
        """主会话配置删除后结束所属 Agent 对话和后台任务，清除待处理输入并释放共享连接。

        Args:
            session_id (str): 已删除配置的主会话编号。
        """
        try:
            # 1. 结束所属 Agent 对话和后台任务，并废弃全部子 Agent。
            operations = [self._retire_children(session_id), self.task_supervisor.stop_session_tasks(session_id)]
            operations.extend(session.close("父配置删除") for session in self._sessions.values()
                if session.context_id == session_id or (
                    isinstance(session, SubagentSession) and session.definition.session_id == session_id))
            results = await asyncio.gather(*operations, return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException):
                    logger.error("父删除后关闭所属资源失败：%s，%s", session_id, result)
            # 2. 清除该主会话尚未处理的输入。
            manager = self._manager_factory(session_id)
            try:
                await manager.discard_pending_inputs()
            finally:
                await manager.close()
            # 3. 释放该主会话使用的共享连接。
            if self._cleanup_session_connections:
                await self._cleanup_session_connections(session_id)
        except Exception:
            logger.exception("父配置删除后的资源收尾失败：%s", session_id)
        finally:
            # 4. 资源清理结束后，移除该主会话的清理任务登记。
            self._parent_cleanup_workers.pop(session_id, None)

    async def _retire_children(self, session_id: str) -> None:
        """主会话配置删除后废弃其所属的全部子 Agent。

        Args:
            session_id (str): 已删除配置的主会话编号。
        """
        records = await self._subagent_dao.list_records(session_id)
        for record in records:
            identifier = record.definition.subagent_id
            await self._subagent_dao.retire(identifier)
            if identifier not in self._sessions:
                self._subagent_records.pop(identifier, None)

    async def begin_shutdown(self) -> None:
        """应用退出时停止受理新的交互命令、子 Agent 请求和插件请求，并通知所有 Agent 对话关闭。"""
        async with self._lifecycle_lock:
            if self.phase in {RuntimePhase.STOPPING, RuntimePhase.STOPPED}:
                return
            self.phase = RuntimePhase.STOPPING
            for session in self._sessions.values():
                session.begin_close()

    async def wait_for_workers(self) -> None:
        """应用退出时关闭所有 Agent 对话，并等待主会话删除后的资源清理完成。"""
        # 会话关闭期间仍可能发送执行结束和审批通知，运行时请求队列处理循环继续处理这些事件。
        results = await asyncio.gather(*(session.close("应用退出") for session in self._sessions.values()),
            *self._parent_cleanup_workers.values(), return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                logger.error("会话关闭失败：%s", result)

    async def close(self) -> None:
        """结束后端运行时，等待会话资源释放和已收到的请求处理完成。"""
        # 1. 停止受理新操作并等待 Agent 对话完成关闭。
        await self.begin_shutdown()
        await self.wait_for_workers()
        # 2. 等待已收到的请求和事件处理完成，再结束运行时请求队列处理循环。
        if self._task_reader is not None and not self._task_reader.done():
            await self.runtime_queue.join()
            self.runtime_queue.put_nowait(None)
            await self._task_reader
        # 3. 将后端运行阶段设为已停止。
        self._task_reader = None
        self.phase = RuntimePhase.STOPPED
