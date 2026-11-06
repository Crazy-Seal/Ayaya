"""组织单个父或子上下文的命令、执行、暂停与资源交还。"""

from __future__ import annotations

import asyncio
from contextlib import aclosing
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
import logging
import sqlite3
from typing import AsyncIterator, Callable
from uuid import uuid4

from app.agent.agent import Agent
from app.agent.core.events import AgentEvent, EventType
from app.agent.runtime.agent_run_state_machine import AgentRunStateMachine
from app.agent.runtime.session_inbox import InboxEnvelope
from app.agent.runtime.requests import RuntimeRequest
from app.agent.runtime.turn_control import TurnControl
from app.agent.runtime.subagent_requests import SubagentReceipt
from app.agent.runtime.tasks.task_supervisor import TaskSupervisor
from app.agent.state.manager import SessionStateManager
from app.agent.state.messages import ImageContent
from app.agent.utils.infra.owned_work import complete_owned_work
from app.interaction.interaction_contract import ChannelCommand, CommandReceipt, InteractionEvent
from app.schemas.runtime_types import (AgentRunOutcome, AgentRunOutcomeKind, ApprovalDecisionKind,
    AuthorizationResolutionKind, CommandKind, DESKTOP_CHANNEL, InboxKind, InboxStatus,
    InteractionEventKind, InterruptKind, RuntimePhase, SessionPhase, TurnInterruptReason, TurnPhase)
from app.schemas.subagent_types import (SubagentDefinition, SubagentInteractionKind, SubagentCommandEffect,
    SubagentPublicStatus, SubagentSummary)
from app.services.agent_identity_service import AgentIdentityService
from app.services.settings_mutation import SettingsMutationCoordinator
from app.services.subagent_service import SubagentService
from app.schemas.task_types import PluginRequest, PluginRequestKind

logger = logging.getLogger(__name__)
INPUT_DELIVERY_MAX_ATTEMPTS = 4
AGENT_CLEANUP_RETRY_SECONDS = 1
AGENT_TO_INTERACTION_EVENT = {
    EventType.TEXT_CHUNK: InteractionEventKind.TEXT_CHUNK,
    EventType.TOOL_CALL: InteractionEventKind.TOOL_CALL,
    EventType.TOOL_COMPLETED: InteractionEventKind.TOOL_COMPLETED,
    EventType.INTERRUPT: InteractionEventKind.INTERRUPT,
    EventType.ERROR: InteractionEventKind.ERROR,
    EventType.DONE: InteractionEventKind.RUN_COMPLETED,
}


@dataclass
class CurrentRun:
    """保存当前轮次的身份和展示材料，执行阶段由会话状态机提供。

    Attributes:
        run_id (str): 当前回答身份，审批恢复沿用，引导后更换。
        origin (str): 本轮回答所属渠道。
        control (TurnControl): 绑定此轮且借用共享状态机的控制对象。
        approval (dict | None): 当前真人审批已保存的展示材料。
        started (bool): 是否已经展示本段开始，用于交付重试期间合并开始通知。
    """
    run_id: str
    origin: str
    control: TurnControl
    approval: dict | None = None
    started: bool = False


@dataclass
class Delegation:
    """保存跨引导轮次继续的一次委派结果及其独占资源。

    Attributes:
        definition (SubagentDefinition): 固定子配置及父归属。
        manager (SessionStateManager): 委派内借给 Agent 的持久状态对象。
        agent (Agent | None): 委派内复用的实例，由此会话释放。
        stop_requested (bool): 已受理的停止意图，阻止继续消费指令。
        finalizing (bool): 已确认结束委派，拒绝再受理引导。
        result_ref (str | None): 当前执行段的持久结果编号。
        outcome (AgentRunOutcome | None): 原执行交还的结果，清理重试时保留。
        cleanup_error (str | None): 本 owner 尚未处理成功的收尾错误。
        started_at (datetime): 此次委派受理时间，覆盖其所有引导轮次。
    """
    definition: SubagentDefinition
    manager: SessionStateManager
    agent: Agent | None = None
    stop_requested: bool = False
    finalizing: bool = False
    result_ref: str | None = None
    outcome: AgentRunOutcome | None = None
    cleanup_error: str | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class AgentSession:
    """持有一个上下文的状态机，组织完整业务并由唯一 worker 执行。

    Attributes:
        run_state (AgentRunStateMachine): 多轮共用的唯一运行阶段。
        current (CurrentRun | None): 当前轮次身份；等待期间保持。
        subagent (Delegation | None): 活动委派资源，父会话及空闲子为空。
        lock (asyncio.Lock): 命令受理与最终交还的短锁。
        wakeup (asyncio.Event): 输入或决定提交后提醒 worker 重新查询。
    """

    def __init__(self, context_id: str, *, identity: AgentIdentityService,
                 mutations: SettingsMutationCoordinator, subagents: SubagentService,
                 agent_factory: Callable[[str, SessionStateManager], Agent],
                 subagent_factory: Callable[[SubagentDefinition, SessionStateManager], Agent] | None,
                 manager_factory: Callable[[str], SessionStateManager],
                 task_supervisor: TaskSupervisor | None, runtime_queue: asyncio.Queue[RuntimeRequest]) -> None:
        """接收执行、持久化和输出依赖，供本上下文的完整业务流程使用。

        Args:
            context_id (str): 本会话实际接收输入和保存历史的编号。
            identity (AgentIdentityService): 共享的身份资格查询对象。
            mutations (SettingsMutationCoordinator): 与配置删除及任务登记共用的受理锁。
            subagents (SubagentService): 子身份、消息和报告的共享业务对象。
            agent_factory (Callable[[str, SessionStateManager], Agent]): 按最新配置装配父 Agent 的工厂。
            subagent_factory (Callable[[SubagentDefinition, SessionStateManager], Agent] | None): 按固定子定义装配 Agent 的工厂。
            manager_factory (Callable[[str], SessionStateManager]): 为会话建立隔离状态业务对象的工厂。
            task_supervisor (TaskSupervisor | None): 普通后台任务的共享执行与清理拥有者。
            runtime_queue (asyncio.Queue[RuntimeRequest]): 应用共享请求及已提交事件队列，由 Scheduler 独占消费。
        """
        self.context_id = context_id
        self.identity, self.mutations, self.subagents = identity, mutations, subagents
        self.agent_factory, self.subagent_factory, self._manager_factory = agent_factory, subagent_factory, manager_factory
        self.task_supervisor, self.runtime_queue = task_supervisor, runtime_queue
        self.run_state = AgentRunStateMachine()
        self.worker: asyncio.Task[None] | None = None
        self.wakeup, self.lock = asyncio.Event(), asyncio.Lock()
        self.activated = False
        self.sequence = 0
        self.current: CurrentRun | None = None
        self.resume_request_id: str | None = None
        self.shutdown_partial: tuple[str, bool, str, str | None] | None = None
        self.subagent: Delegation | None = None
        self.cleanup_agents: list[Agent] = []
        self._closing = False
        self._next_run_id: str | None = None
        self.last_outcome = AgentRunOutcome(AgentRunOutcomeKind.STOPPED)

    def _new_manager(self, context_id: str) -> SessionStateManager:
        """创建本上下文的状态对象并注入共享资格及受理依赖。

        Args:
            context_id (str): 本会话实际接收输入和保存历史的编号。
        """
        manager = self._manager_factory(context_id)
        manager.bind_runtime(self.identity, self.mutations, self.context_id)
        return manager

    @property
    def phase(self) -> SessionPhase:
        """将运行事实投影为现有界面阶段。"""
        if self.current is None or (self.subagent is None and self.run_state.phase == TurnPhase.FINISHING):
            return SessionPhase.IDLE
        return SessionPhase.PENDING_INTERRUPT if self.current.approval is not None else SessionPhase.RUNNING

    @property
    def waiting_parent(self) -> bool:
        """从共享状态机读取本上下文是否正在等待父消息。"""
        return self.run_state.phase == TurnPhase.INTERRUPTED and self.run_state.interrupt_kind == InterruptKind.PARENT_MESSAGE

    @property
    def runtime_phase(self) -> RuntimePhase:
        """返回本上下文是否已经停止新执行。"""
        return RuntimePhase.STOPPING if self._closing else RuntimePhase.RUNNING

    def _emit(self, event: InteractionEvent) -> None:
        """把已提交的命令结果交给应用原有请求队列，由 Scheduler 统一处理展示。

        Args:
            event (InteractionEvent): 持久化成功后需要展示或路由的事件。
        """
        self.runtime_queue.put_nowait(event)

    def _end_run(self) -> None:
        """旧轮执行已经交还，先登记收尾再清除轮次身份。"""
        if self.run_state.run_id is not None:
            self.run_state.begin_finishing(self.run_state.run_id)
        self.current = None

    def _idle(self) -> None:
        """报告与执行资源交还完成后确认上下文空闲。"""
        self._end_run()
        if self.run_state.run_id is not None:
            self.run_state.complete(self.run_state.run_id)
        self._next_run_id = None

    def snapshot(self, definition: SubagentDefinition) -> SubagentSummary:
        """复制现有公开摘要，等待原因与审批从本上下文推导。

        Args:
            definition (SubagentDefinition): 已确认父子归属的固定子定义。
        """
        activity = self.subagent
        status = (SubagentPublicStatus.IDLE if activity is None else
                  SubagentPublicStatus.WAITING_PARENT if self.waiting_parent else SubagentPublicStatus.RUNNING)
        return SubagentSummary(definition.subagent_id, definition.name, definition.if_retain_context, status,
            activity.started_at.isoformat() if activity else None, definition.created_at.isoformat(),
            bool(self.current and self.current.approval), activity.cleanup_error if activity else None)

    def start_delegation(self, definition: SubagentDefinition, *, cleanup_only: bool = False) -> Delegation:
        """受理成功后建立活动资源；实际 worker 由 Scheduler 登记。

        Args:
            definition (SubagentDefinition): 已确认父子归属的固定子定义。
            cleanup_only (bool): 是否为已废弃身份建立仅清理资源的活动。
        """
        if self.subagent is None:
            manager = self.subagents.new_manager(definition)
            manager.bind_runtime(self.identity, self.mutations, definition.session_id)
            self.subagent = Delegation(definition, manager, finalizing=cleanup_only)
        self.wakeup.set()
        return self.subagent

    async def accept_command(self, definition: SubagentDefinition, text: str, source_id: str) -> SubagentReceipt:
        """保存父命令后请求引导或建立委派，调用方只需登记唯一 worker。

        Args:
            definition (SubagentDefinition): 已核对归属的目标定义。
            text (str): 父的新指令。
            source_id (str): 重试期间固定的调用来源。

        Returns:
            SubagentReceipt: 已保存命令的受理动作、当时状态及已有结果引用。
        """
        async with self.lock:
            await self.identity.require_available(definition.session_id, definition.subagent_id)
            if not text or not text.strip():
                raise ValueError("命令正文不能为空")
            activity = self.subagent
            if activity is not None and (activity.finalizing or activity.stop_requested):
                return SubagentReceipt(self.context_id, False, SubagentCommandEffect.FINALIZING, self.snapshot(definition))
            waiting = self.waiting_parent
            kind = (SubagentInteractionKind.REPLY if waiting else SubagentInteractionKind.STEER
                    if activity is not None else SubagentInteractionKind.TASK)
            # 1. 保存成功才改变执行；父回复与普通引导使用同一输入路径。
            accepted_kind, created = await self.subagents.definitions.append_message(definition, kind, text, source_id)
            if not created:
                effect = {SubagentInteractionKind.TASK: SubagentCommandEffect.STARTED,
                          SubagentInteractionKind.STEER: SubagentCommandEffect.STEERED,
                          SubagentInteractionKind.REPLY: SubagentCommandEffect.REPLIED}[accepted_kind]
                if activity is not None:
                    return SubagentReceipt(self.context_id, True, effect, self.snapshot(definition),
                        result_ref=activity.result_ref)
                previous = await self.subagents.latest_result(definition)
                return SubagentReceipt(self.context_id, True, effect, self.snapshot(definition),
                    result_ref=previous.task_id if previous else None)
            if activity is None:
                activity = self.start_delegation(definition)
                effect = SubagentCommandEffect.STARTED
            else:
                if self.current is not None and self.current.approval is None:
                    self.current.control.request(TurnInterruptReason.STEER)
                effect = SubagentCommandEffect.REPLIED if waiting else SubagentCommandEffect.STEERED
            self.wakeup.set()
            await self._publish_subagent(definition)
            return SubagentReceipt(self.context_id, True, effect, self.snapshot(definition),
                                   result_ref=activity.result_ref)

    async def accept_created_identity(self, definition: SubagentDefinition, *, created: bool) -> SubagentReceipt:
        """为首次创建登记活动，为重放创建返回已有执行或结果的受理回执。

        Args:
            definition (SubagentDefinition): 已确认父子归属的固定子定义。
            created (bool): 创建事务是否首次写入定义，重放创建时为 False。
        """
        async with self.lock:
            record = await self.subagents.definitions.get(definition.session_id, definition.subagent_id)
            if record.retired_at is not None:
                return SubagentReceipt(self.context_id, True, SubagentCommandEffect.RELEASED, None)
            activity = self.subagent
            if activity is None and (created or await self.subagents.pending_creation(definition)):
                activity = self.start_delegation(definition)
            if activity is not None:
                await self._publish_subagent(definition)
                return SubagentReceipt(self.context_id, True, SubagentCommandEffect.STARTED, self.snapshot(definition),
                                       result_ref=activity.result_ref)
            previous = await self.subagents.latest_result(definition)
            return SubagentReceipt(self.context_id, True, SubagentCommandEffect.STARTED, self.snapshot(definition),
                                   result_ref=previous.task_id if previous else None)

    async def request_stop(self, definition: SubagentDefinition) -> SubagentReceipt:
        """结束活动委派，原 worker 完成保存、报告与资源清理。

        Args:
            definition (SubagentDefinition): 已确认父子归属的固定子定义。
        """
        async with self.lock:
            activity = self.subagent
            if activity is None:
                return SubagentReceipt(self.context_id, True, SubagentCommandEffect.IDLE, self.snapshot(definition))
            activity.stop_requested = True
            if self.current is not None and self.current.approval is not None:
                await self._cancel_pending_interrupt(self.context_id, DESKTOP_CHANNEL)
            if self.current is not None:
                self.current.control.request(TurnInterruptReason.STOP)
            self.wakeup.set()
            return SubagentReceipt(self.context_id, True, SubagentCommandEffect.STOPPING, self.snapshot(definition),
                                   result_ref=activity.result_ref)

    async def destroy(self, definition: SubagentDefinition) -> SubagentReceipt:
        """废弃身份后请求原 owner 清理，重复销毁复用活动资源。

        Args:
            definition (SubagentDefinition): 已确认父子归属的固定子定义。
        """
        async with self.lock:
            # 1. 父已删除时仍允许幂等废弃，归属由路由读取的定义确认。
            await self.subagents.definitions.retire(definition.subagent_id)
            if self.current is not None and self.current.approval is not None:
                request_id = self.current.approval.get("request_id")
                self.current.approval = None
                self._emit(InteractionEvent(self.context_id, InteractionEventKind.AUTHORIZATION_RESOLVED,
                    {"outcome": AuthorizationResolutionKind.CANCELLED}, request_id=request_id))
            activity = self.start_delegation(definition, cleanup_only=True)
            self.request_shutdown()
            await self._publish_subagent(definition)
            return SubagentReceipt(self.context_id, True, SubagentCommandEffect.RELEASED, None,
                                   result_ref=activity.result_ref)

    def request_shutdown(self, *, application_exit: bool = False) -> None:
        """关闭执行资格并保护已经开始的最终提交和清理。

        Args:
            application_exit (bool): 是否正在关闭整个运行时，退出时沿统一补记流程保存正文。
        """
        if application_exit:
            self._closing = True
        self.activated = False
        activity = self.subagent
        if activity is not None:
            activity.stop_requested = True
        self.wakeup.set()
        control = self.current.control if self.current else None
        if control is not None:
            if activity is not None or not self._closing:
                control.request(TurnInterruptReason.STOP)
            if control.phase == TurnPhase.COMMITTING:
                return
        if activity is not None and (activity.finalizing or activity.agent is None):
            return
        if self.worker is not None and not self.worker.done():
            self.worker.cancel()

    async def run(self) -> AsyncIterator[InteractionEvent]:
        """使用同一循环处理父输入、子命令和审批恢复，暂停时只等待有效输入。

        Yields:
            InteractionEvent: 已保存输入、模型输出或暂停事实，交给 Scheduler 路由。
        """
        activity = self.subagent
        manager = activity.manager if activity else self._new_manager(self.context_id)
        try:
            while not self._closing:
                self.wakeup.clear()
                owner = activity.definition.session_id if activity else self.context_id
                # 1. 暂停被取消后交还父轮次状态，再检查后续执行资格。
                if self.run_state.phase == TurnPhase.FINISHING and activity is None:
                    self._idle()
                async with self.lock:
                    if activity is not None and activity.finalizing:
                        break
                    if activity is not None and activity.stop_requested:
                        activity.outcome = activity.outcome or AgentRunOutcome(AgentRunOutcomeKind.STOPPED, result="子任务已停止")
                        break
                    if not self.identity.settings.exists(owner) or (activity is None and not self.activated):
                        if activity:
                            activity.outcome = AgentRunOutcome(AgentRunOutcomeKind.STOPPED, result="子任务已停止")
                        break
                    request_id = self.resume_request_id
                    self.resume_request_id = None
                    paused = self.run_state.phase == TurnPhase.INTERRUPTED
                    kind = (InboxKind.AGENT_COMMAND if activity else InboxKind.HUMAN) if paused else None
                batch = () if request_id else await manager.read_activation_batch(kind=kind, include_queued=not paused)
                if not batch and request_id is None:
                    if not paused and activity is None:
                        self._idle()
                    await self.wakeup.wait()
                    continue
                # 2. 新命令结束暂停，补齐旧协议后再切换轮次；后台通知不能触发本分支。
                if paused and request_id is None:
                    if not await self._prepare_input_transition(manager):
                        continue
                if activity is not None:
                    if not await self._prepare_run(activity):
                        continue
                async for event in self._execute_run(self.context_id, manager, batch, request_id):
                    yield event
                outcome = self.last_outcome
                if activity is None:
                    continue
                activity.outcome = outcome
                if outcome.kind in {AgentRunOutcomeKind.INTERRUPTED, AgentRunOutcomeKind.STEERED} and not activity.stop_requested:
                    continue
                # 3. 完成判断和命令受理共用短锁，已受理的引导仍属于当前委派。
                async with self.lock:
                    pending = await manager.read_activation_batch()
                    if (outcome.kind != AgentRunOutcomeKind.FAILED and not activity.stop_requested
                            and any(item.kind == InboxKind.AGENT_COMMAND for item in pending)):
                        continue
                    activity.finalizing = True
                    break
        except asyncio.CancelledError:
            if activity is not None:
                activity.outcome = AgentRunOutcome(AgentRunOutcomeKind.STOPPED, result="子任务已停止")
        except Exception as exc:
            logger.exception("上下文执行失败：%s", self.context_id)
            if activity is not None:
                activity.outcome = AgentRunOutcome(AgentRunOutcomeKind.FAILED, error=str(exc))
            elif not self.identity.settings.exists(self.context_id):
                raise
            else:
                self.activated = False
                await self._record_failure(self.context_id, DESKTOP_CHANNEL, self.run_state.run_id, exc)
        finally:
            # 4. 原 owner 统一处理退出，父上下文交还本段资源，子上下文提交报告并按保留策略清理。
            if activity is not None:
                activity.finalizing = True
                await self._finish_activity(self.context_id, activity)
            else:
                try:
                    self._idle()
                    await manager.close()
                except Exception:
                    logger.exception("关闭会话输入连接失败：%s", self.context_id)

    async def _prepare_input_transition(self, manager: SessionStateManager) -> bool:
        """由原 worker 整理被新输入替代的暂停，保存失败保留输入并在此重试。

        Args:
            manager (SessionStateManager): 当前 worker 拥有的状态与协议业务对象。

        Returns:
            bool: 旧轮整理完成可继续交付；退出或停止时结束本次准备。
        """
        while not self._closing:
            try:
                async with self.lock:
                    if self.subagent is not None and self.subagent.stop_requested:
                        return False
                    if self.subagent is None and not self.activated:
                        return False
                    # 1. 取消决定成功后发布事件，让全局审批队列推进；重试保留未完成的步骤。
                    if self.current is not None and self.current.approval is not None:
                        await self._cancel_pending_interrupt(self.context_id, DESKTOP_CHANNEL)
                    # 2. 先保存旧工具的结束结果，新输入仍 pending，下一步才切换结果段并交付。
                    await manager.close_previous_tools(await manager.load())
                    self._end_run()
                return True
            except (sqlite3.Error, OSError):
                logger.exception("会话 %s 整理旧暂停失败，保留新输入等待重试", self.context_id)
                await asyncio.sleep(AGENT_CLEANUP_RETRY_SECONDS)
        return False

    async def _publish_subagent(self, definition: SubagentDefinition) -> None:
        """提交后发布身份与运行摘要，显示异常由日志记录。

        Args:
            definition (SubagentDefinition): 已确认父子归属的固定子定义。
        """
        try:
            record = await self.subagents.definitions.get(definition.session_id, definition.subagent_id)
            payload = ({**asdict(self.snapshot(definition)), "removed": False} if record and record.retired_at is None
                       else {"subagent_id": definition.subagent_id, "removed": True})
            self._emit(InteractionEvent(definition.session_id, InteractionEventKind.SUBAGENT_STATUS, payload))
        except Exception:
            logger.exception("发布子 Agent 状态失败：%s", definition.subagent_id)

    def matching_control(self, run_id: str | None) -> TurnControl | None:
        """在会话锁内查找目标轮次的控制对象，HTTP 和插件请求共同使用。

        Args:
            run_id (str | None): 固定目标轮次；HTTP 未指定时使用当前轮次。

        Returns:
            TurnControl | None: 仍在执行且编号匹配的轮次，审批暂停或目标过期时为空。
        """
        session = self
        current = session.current
        control = current.control if current else None
        if control is None or session.phase != SessionPhase.RUNNING or (run_id and control.run_id != run_id):
            return None
        return control

    def request_interrupt(self, run_id: str | None,
                                reason: TurnInterruptReason) -> CommandReceipt:
        """复用目标核验和最终打断动作，用户暂停资格由入口按来源处理。

        Args:
            run_id (str | None): 请求控制的轮次，插件始终提供固定来源编号。
            reason (TurnInterruptReason): 停止或引导，控制对象保留首个原因。

        Returns:
            CommandReceipt: 内部受理结果，HTTP 可以返回，插件入口仅写入日志。
        """
        control = self.matching_control(run_id)
        if control is None:
            return CommandReceipt(False, "没有匹配的活动轮次")
        if not control.request(reason):
            return CommandReceipt(False, "当前轮次正在提交，已无法打断")
        return CommandReceipt(True, run_id=control.run_id)

    async def accept_human(self, command: ChannelCommand) -> CommandReceipt:
        """先保存真人输入，再打断回答或取消审批，最后唤醒 worker。

        Args:
            command (ChannelCommand): 普通消息、引导或排队命令。

        Returns:
            CommandReceipt: 输入的受理结果及当前轮次编号，后续交付由 worker 执行。
        """
        if command.kind == CommandKind.HUMAN_STEER and not command.target_run_id:
            return CommandReceipt(False, "引导缺少目标轮次")
        message, images = command.payload.get("message"), command.payload.get("images")
        if not isinstance(message, str) or (not message.strip() and not images):
            return CommandReceipt(False, "消息不能为空")
        session = self
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
        manager = self._new_manager(command.session_id)
        try:
            envelope = await manager.submit_human(command.origin_channel, message, images=images, queued=queued)
        finally:
            await manager.close()
        # 2. 保存失败不会打断旧回答；保存成功后获得本次启动的执行资格。
        session.activated = True
        if not queued and phase == SessionPhase.RUNNING and control is not None:
            if command.kind != CommandKind.HUMAN_STEER or control.run_id == command.target_run_id:
                self.request_interrupt(control.run_id, TurnInterruptReason.STEER)
        if queued:
            self._emit(InteractionEvent(command.session_id, InteractionEventKind.QUEUED_MESSAGE,
                {"input_sequence": envelope.id, "message": message, "images": images or []},
                origin_channel=command.origin_channel))
        if not queued and phase == SessionPhase.PENDING_INTERRUPT:
            await self._cancel_pending_interrupt(command.session_id, command.origin_channel)
        self.wakeup.set()
        current = session.current
        return CommandReceipt(True, run_id=current.run_id if current else None)

    async def cancel_queued(self, command: ChannelCommand) -> CommandReceipt:
        """删除本渠道尚未交付的排队消息，提交成功才产生移除事件。

        Args:
            command (ChannelCommand): 携带 input_sequence 的撤销命令。

        Returns:
            CommandReceipt: 是否删除；已交付或不存在的输入不能撤销。
        """
        sequence = command.payload.get("input_sequence")
        if not isinstance(sequence, int):
            return CommandReceipt(False, "缺少排队消息编号")
        manager = self._new_manager(command.session_id)
        try:
            cancelled = await manager.cancel_queued(sequence, command.origin_channel)
        finally:
            await manager.close()
        if cancelled:
            self._emit(InteractionEvent(command.session_id, InteractionEventKind.QUEUED_CANCELLED,
                {"input_sequence": sequence}, origin_channel=command.origin_channel))
        return CommandReceipt(cancelled, None if cancelled else "排队消息已交付或不存在")

    async def stop_current_turn(self, command: ChannelCommand) -> CommandReceipt:
        """停止匹配的活动回答或取消审批等待，暂停后续自动执行。

        Args:
            command (ChannelCommand): 可选携带目标回答编号。

        Returns:
            CommandReceipt: 已受理，或无匹配轮次、正在最终提交。
        """
        if self.current is not None and self.current.approval is not None:
            run_id = self.current.run_id
            if command.target_run_id and command.target_run_id != run_id:
                return CommandReceipt(False, "没有匹配的活动轮次")
            await self._cancel_pending_interrupt(command.session_id, command.origin_channel)
            receipt = CommandReceipt(True, run_id=run_id)
        else:
            receipt = self.request_interrupt(command.target_run_id, TurnInterruptReason.STOP)
        if not receipt.accepted:
            return receipt
        # 1. 用户停止成功后暂停后续自动执行，通知继续保存，待下次真人输入恢复。
        self.activated = False
        self.wakeup.set()
        return receipt

    async def _cancel_pending_interrupt(self, session_id: str, actor: str) -> None:
        """新输入替代旧审批或用户停止等待时保存取消；调用方已经持会话锁。

        Args:
            session_id (str): 新输入所属会话。
            actor (str): 取消审批的来源渠道。
        """
        session = self
        manager = self.subagent.manager if self.subagent else self._new_manager(session_id)
        try:
            cancelled = await manager.cancel_pending_interrupt()
            if cancelled is None:
                return
            request_id, origin = cancelled
            run_id = session.current.run_id if session.current else None
            # 1. 决定保存成功后同步结束旧轮次，连续发布取消及完成通知。
            session._end_run()
            self._emit(InteractionEvent(session_id, InteractionEventKind.AUTHORIZATION_RESOLVED,
                {"outcome": AuthorizationResolutionKind.CANCELLED, "actor_channel": actor},
                origin_channel=origin, request_id=request_id))
            self._emit(InteractionEvent(session_id, InteractionEventKind.RUN_COMPLETED,
                {"value": {"interrupted": True}}, origin_channel=origin, run_id=run_id))
        finally:
            if self.subagent is None:
                await manager.close()

    async def resolve_authorization(self, command: ChannelCommand) -> CommandReceipt:
        """锁内重读并保存首次决定，再安排原轮恢复或交付新指令。

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
        session = self
        execution = session.subagent
        manager = execution.manager if execution is not None else self._new_manager(command.session_id)
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
                session._end_run()
                session.activated = execution is None
                self._emit(InteractionEvent(command.session_id, InteractionEventKind.RUN_COMPLETED,
                    {"value": {"interrupted": True}}, origin_channel=origin, run_id=run_id))
            else:
                if session.current is not None:
                    session.run_state.prepare_resume(session.current.run_id, command.request_id)
                session.resume_request_id = command.request_id
        finally:
            if execution is None:
                await manager.close()
        self.wakeup.set()
        return CommandReceipt(True, run_id=session.current.run_id if session.current else None)

    def _acquire_agent(self, manager: SessionStateManager, origin: str,
                       run_id: str | None, control: TurnControl | None) -> tuple[Agent, str, TurnControl]:
        """父按执行段装配、子按委派复用，轮次控制借用同一状态机。"""
        activity = self.subagent
        if activity is not None:
            if activity.agent is None:
                if self.subagent_factory is None:
                    raise RuntimeError("未装配子 Agent 工厂")
                activity.agent = self.subagent_factory(activity.definition, manager)
            agent = activity.agent
            if self.current is not None:
                run_id, control = self.current.run_id, self.current.control
        else:
            agent = self.agent_factory(self.context_id, manager)
        if control is None:
            run_id = run_id or uuid4().hex
            control = TurnControl(run_id, run_state=self.run_state)
            self.current = CurrentRun(run_id, origin, control)
        return agent, control.run_id, control

    async def _finish_pending_response(self, agent: Agent, outcome: AgentRunOutcome) -> AgentRunOutcome:
        """原回答保存失败时持有同一实例重试，退出后交给统一退出补记。

        Args:
            agent (Agent): 仍由本 worker 持有的实际执行实例。
            outcome (AgentRunOutcome): 原执行产生的正文、结束原因或失败信息。
        """
        while True:
            try:
                return await agent.finish_pending_response() or outcome
            except asyncio.CancelledError:
                outcome = agent.last_outcome or outcome
            except Exception:
                if self._closing:
                    logger.exception("会话 %s 退出时回答保存失败，继续资源关闭及退出补记", self.context_id)
                    return outcome
                logger.exception("会话 %s 回答保存失败，保留原执行等待重试", self.context_id)
                try:
                    await asyncio.sleep(AGENT_CLEANUP_RETRY_SECONDS)
                except asyncio.CancelledError:
                    pass

    async def _release_segment_agent(self, agent: Agent) -> None:
        """原 worker 重试父资源释放；删除或退出后仍保留失败句柄供统一清理。

        Args:
            agent (Agent): 仍由本 worker 持有的实际执行实例。
        """
        while True:
            try:
                await complete_owned_work(agent.close())
                return
            except asyncio.CancelledError:
                if self._closing:
                    self.cleanup_agents.append(agent)
                    return
            except Exception:
                logger.exception("关闭会话 %s 的 Agent 失败", self.context_id)
                if not self.identity.settings.exists(self.context_id):
                    self.cleanup_agents.append(agent)
                    raise
                if self._closing:
                    self.cleanup_agents.append(agent)
                    return
            try:
                await asyncio.sleep(AGENT_CLEANUP_RETRY_SECONDS)
            except asyncio.CancelledError:
                if self._closing:
                    self.cleanup_agents.append(agent)
                    return

    async def _execute_run(
        self, session_id: str, manager: SessionStateManager, batch: tuple[InboxEnvelope, ...], request_id: str | None,
    ) -> AsyncIterator[InteractionEvent]:
        """创建执行并交付输入，交付前失败至多重试四次，交付后失败交给保存和收尾流程。

        Args:
            session_id (str): 当前会话。
            manager (SessionStateManager): worker 拥有并借给本次 Agent 的会话状态业务对象。
            batch (tuple[InboxEnvelope, ...]): 准备交付的输入，审批恢复时为空。
            request_id (str | None): 从 checkpoint 恢复的审批请求。

        Yields:
            InteractionEvent: 执行期间的已提交事件；结束结果保存在 last_outcome 供原 worker 读取。
        """
        session = self
        human = next((item for item in batch if item.kind == InboxKind.HUMAN), None)
        terminals = tuple(item for item in batch if item.kind != InboxKind.HUMAN)
        origin = human.origin_channel if human else DESKTOP_CHANNEL
        run_id = None
        control = None
        paused_for_approval = False
        outcome = AgentRunOutcome(AgentRunOutcomeKind.STOPPED)
        try:
            # 1. 恢复只传请求编号；状态、持久决定和有效性由 Agent 自行加载核验。
            if request_id is not None and session.current is not None:
                origin = session.current.origin
                run_id = session.current.run_id
            attempts = INPUT_DELIVERY_MAX_ATTEMPTS if batch else 1
            for attempt in range(attempts):
                agent = None
                execution_error = None
                try:
                    # 2. 父输入重试创建独立 Agent，子活动任务复用原实例；轮次身份和控制继续沿用。
                    async with self.mutations.admission_lock, session.lock:
                        # 2.1. 删除提交后不能开始新的模型执行。
                        owner_id = session.subagent.definition.session_id if session.subagent else session_id
                        if session.subagent is not None and session.subagent.stop_requested:
                            break
                        if not await self.identity.read_availability(owner_id, session_id if session.subagent else None):
                            break
                        # 2.2. 输入交付重试也遵守用户停止；随后真人输入恢复的资格照常保留。
                        if not session.activated and session.subagent is None:
                            # 2.2.1. 当前轮次可能已通过事件或状态查询显示为执行中；结束后通知界面，输入留待真人恢复。
                            if session.current is not None:
                                self._emit(InteractionEvent(session_id, InteractionEventKind.RUN_COMPLETED,
                                    {"value": {"interrupted": True}},
                                    origin_channel=session.current.origin, run_id=session.current.run_id))
                                session._end_run()
                            break
                        agent, run_id, control = self._acquire_agent(manager, origin, run_id, control)
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
                    async for event in self._consume_events(session_id, origin, run_id, stream):
                        if event.kind == InteractionEventKind.INTERRUPT:
                            paused_for_approval = True
                        yield event
                    outcome = agent.last_outcome or AgentRunOutcome(
                        AgentRunOutcomeKind.INTERRUPTED if paused_for_approval else AgentRunOutcomeKind.STOPPED)
                    break
                except asyncio.CancelledError:
                    # 3. worker 取消先结束原事件流；Agent 已保存的正文通过统一结果交还。
                    outcome = (agent.last_outcome if agent is not None else None) or AgentRunOutcome(
                        AgentRunOutcomeKind.STOPPED, result="子任务已停止" if session.subagent else "当前执行已停止")
                    break
                except Exception as exc:
                    # 4. 仅异常路径核对交付事实；已保存输入不能因为后续执行失败重新运行。
                    item = human or (batch[0] if batch else None)
                    # 4.1. 查询持有数据库游标；退出取消先等查询释放，再开始中断保存或退出补记。
                    status = await complete_owned_work(manager.status(item.id)) if item is not None else InboxStatus.DELIVERED
                    if status is None:
                        async with session.lock:
                            session._end_run()
                            if run_id is not None:
                                self._emit(InteractionEvent(session_id, InteractionEventKind.RUN_COMPLETED,
                                    {"value": {"input_cancelled": True}}, origin_channel=origin, run_id=run_id))
                        outcome = AgentRunOutcome(AgentRunOutcomeKind.STOPPED)
                        break
                    if status == InboxStatus.DELIVERED:
                        logger.exception("会话 %s 已交付输入后的执行失败", session_id)
                        execution_error = exc
                        outcome = AgentRunOutcome(AgentRunOutcomeKind.FAILED, error=str(exc))
                        break
                    if attempt + 1 < attempts:
                        logger.warning("会话 %s 输入交付失败，第 %s 次尝试：%s", session_id, attempt + 1, exc)
                        continue
                    # 4.2. 耗尽后先移除失败真人输入，再一次性报告；任务通知保持待交付事实。
                    async with session.lock:
                        failed = None
                        if human is not None:
                            await manager.discard_human_input(human.id)
                            failed = {"input_sequence": human.id, "message": human.payload["message"],
                                      "images": human.payload.get("images") or []}
                        session.activated = False
                        session._end_run()
                        payload = {"detail": str(exc)}
                        if failed is not None:
                            payload["failed_input"] = failed
                        self._emit(InteractionEvent(session_id, InteractionEventKind.ERROR, payload,
                            origin_channel=origin, run_id=run_id))
                    outcome = AgentRunOutcome(AgentRunOutcomeKind.FAILED, error=str(exc))
                    break
                finally:
                    # 5. 父子执行都先保存原回答；原 worker 保留 Agent 及待保存材料，失败后继续提交。
                    if agent is not None:
                        outcome = await self._finish_pending_response(agent, outcome)
                    # 6. 保存恢复后的停止结果不报告为业务失败；真实执行失败只通知一次。
                    if execution_error is not None:
                        if outcome.kind == AgentRunOutcomeKind.FAILED:
                            await self._record_failure(session_id, origin, run_id, execution_error)
                        else:
                            # 6.1. 原事件流因保存错误结束；Agent 交还已保存结果后补发完成通知。
                            paused_for_approval = False
                            async with session.lock:
                                self._emit(InteractionEvent(session_id, InteractionEventKind.RUN_COMPLETED,
                                    {"value": None}, origin_channel=origin, run_id=run_id))
                    # 7. 原 worker 交还父 Agent 的插件和模型资源，子 Agent 继续由活动委派持有。
                    if agent is not None and session.subagent is None:
                        await self._release_segment_agent(agent)

        except Exception as exc:
            logger.exception("会话 %s 的执行准备失败", session_id)
            if not self.identity.settings.exists(session_id):
                raise
            await self._record_failure(session_id, origin, run_id, exc)
            session.activated = False
            outcome = AgentRunOutcome(AgentRunOutcomeKind.FAILED, error=str(exc))
        finally:
            # 8. 清除本段控制对象；待恢复审批保留原回答身份。
            # 8.1. 按本次执行的暂停事实判断，审批决定可能已改变会话的显示阶段。
            async with session.lock:
                if session.current and session.current.control is control:
                    if not paused_for_approval:
                        session._end_run()
            self.last_outcome = outcome

    async def _consume_events(
        self, session_id: str, origin: str, run_id: str, stream: AsyncIterator[AgentEvent],
    ) -> AsyncIterator[InteractionEvent]:
        """转换已提交的 Agent 事件并记录审批展示材料，阶段仍由共享状态机推进。

        Args:
            session_id (str): 会话编号。
            origin (str): 本轮来源渠道。
            run_id (str): 已开始的回答编号。
            stream (AsyncIterator[AgentEvent]): 本次 Agent 的输出。

        Yields:
            InteractionEvent: 本轮执行的已提交输入、正文或等待事件。
        """
        session = self
        error_reported = False
        try:
            # 显式等待下层迭代关闭，保证响应和借用资源在返回前完成清理。
            async with aclosing(stream) as managed_events:
                async for event in managed_events:
                    async with session.lock:
                        if session.subagent is not None:
                            definition = session.subagent.definition
                            if not await self.identity.read_availability(definition.session_id, session_id):
                                continue
                        elif not await self.identity.read_availability(session_id):
                            continue
                        # 1. 复制本轮已提交的事件材料，执行结果随后由 Agent 交还。
                        if event.type == EventType.RUN_STARTED:
                            # 1.1. Agent 已加载并核验状态，首次开始时发布通知，重试沿用本轮身份。
                            current = session.current
                            if current is not None and not current.started:
                                origin = event.data["origin_channel"]
                                current.origin = origin
                                current.started = True
                                yield InteractionEvent(session_id, InteractionEventKind.RUN_STARTED,
                                    {"resumed": True} if event.data["resumed"] else {},
                                    origin_channel=origin, run_id=run_id)
                            continue
                        if event.type == EventType.HUMAN_INPUT_COMMITTED:
                            # 1.2. 按原通知转出已保存的真人消息，附带编号供界面合并历史。
                            message = event.data["message"]
                            yield InteractionEvent(session_id, InteractionEventKind.HUMAN_MESSAGE,
                                {"message": message.text,
                                 "images": [part.url for part in message.content if isinstance(part, ImageContent)],
                                 "input_sequence": event.data["input_sequence"], "message_id": message.message_id},
                                origin_channel=message.origin_channel)
                            continue
                        if event.type == EventType.INTERRUPT and session.current:
                            if event.data.get("type") == InterruptKind.AUTHORIZATION_REQUEST:
                                session.current.approval = event.data
                        yield InteractionEvent(
                            session_id, AGENT_TO_INTERACTION_EVENT[event.type], {"value": event.data},
                            origin_channel=origin, run_id=run_id,
                            request_id=(event.data or {}).get("request_id") if event.type == EventType.INTERRUPT else None,
                        )
                        if session.subagent is not None and event.type == EventType.INTERRUPT:
                            await self._publish_subagent(session.subagent.definition)
                        error_reported = error_reported or event.type == EventType.ERROR
        except Exception:
            # 2. 未报告的异常交给本轮入口处理；已有错误结果时仅记录后续源流清理失败。
            if not error_reported:
                raise
            logger.exception("会话 %s 已报告错误，结束输出时再次失败", session_id)


    async def _record_failure(self, session_id: str, origin: str, run_id: str | None, exc: Exception) -> None:
        """登记本轮结束并发布执行错误，供界面展示失败原因。

        Args:
            session_id (str): 失败会话。
            origin (str): 本轮来源。
            run_id (str | None): 尚未创建 Agent 时可为空。
            exc (Exception): 本轮失败原因。
        """
        session = self
        async with session.lock:
            session._end_run()
            self._emit(InteractionEvent(session_id, InteractionEventKind.ERROR,
                {"detail": str(exc)}, origin_channel=origin, run_id=run_id))

    async def _prepare_run(self, execution: Delegation) -> bool:
        """切换结果段提交成功后才开始新轮，失败只重试同一个候选编号。

        Args:
            execution (Delegation): 当前活动委派及上一结果引用。

        Returns:
            bool: 可以继续执行；已停止或退出时为 False。
        """
        while not self._closing:
            try:
                async with self.mutations.admission_lock, self.lock:
                    if execution.stop_requested:
                        return False
                    if self.current is not None:
                        return True
                    self._next_run_id = self._next_run_id or uuid4().hex
                    await self.subagents.results.start_subagent_run(execution.definition, self._next_run_id, execution.result_ref)
                    execution.result_ref = self._next_run_id
                    self.current = CurrentRun(self._next_run_id, DESKTOP_CHANNEL,
                                              TurnControl(self._next_run_id, run_state=self.run_state))
                    self._next_run_id = None
                    return True
            except (sqlite3.Error, OSError):
                logger.exception("子轮次切换保存失败，保留旧结果引用重试：%s", self.context_id)
                if self._closing:
                    return False
                await asyncio.sleep(AGENT_CLEANUP_RETRY_SECONDS)
        return False

    async def _ensure_result(self, execution: Delegation) -> None:
        """已受理工作在开始执行前失败或停止时，建立唯一的终态留档引用。

        Args:
            execution (Delegation): 尚未取得结果段编号的已受理委派。
        """
        if execution.result_ref is None and execution.outcome is not None:
            self._next_run_id = self._next_run_id or uuid4().hex
            await self.subagents.results.start_subagent_run(execution.definition, self._next_run_id, None)
            execution.result_ref = self._next_run_id
            self._next_run_id = None

    async def _commit_result(self, execution: Delegation, *, notify: bool) -> None:
        """保存已有执行段的首次终态，只有新通知提交成功才提醒父会话。

        Args:
            execution (Delegation): 原执行持有的结果和 run 引用。
            notify (bool): 调用方在受理锁内确认本次允许向父 Inbox 投递。
        """
        if execution.result_ref is None:
            return
        outcome = execution.outcome or AgentRunOutcome(AgentRunOutcomeKind.STOPPED)
        if await self.subagents.finish_run(execution.result_ref, outcome, notify=notify) and notify:
            record = await self.subagents.results.get(execution.result_ref)
            if record is not None:
                self.runtime_queue.put_nowait(record)

    async def _finish_activity(self, subagent_id: str, execution: Delegation) -> None:
        """处理委派报告及身份废弃，由唯一 worker 重试资源清理。

        Args:
            subagent_id (str): 正在收尾的子身份。
            execution (Delegation): 本次执行持有的资源和结果，重试时继续使用。
        """
        definition = execution.definition
        session = self
        while True:
            execution.cleanup_error = None
            try:
                # 1. 中断保存失败时先重试原正文，保存成功才提交任务终态和释放执行实例。
                if execution.agent is not None:
                    saved_outcome = await execution.agent.finish_pending_response()
                    if saved_outcome is not None:
                        execution.outcome = saved_outcome
                # 1.1. 等待期间停止没有正在运行的 Agent 流，仍由本 owner 补齐工具协议。
                state = await execution.manager.load()
                if state.interrupt_data or state.pending_tool_calls:
                    await execution.manager.finish_interrupted_turn(state, partial="", partial_persisted=False,
                        origin_channel=self.current.origin if self.current else DESKTOP_CHANNEL)
                    if self.run_state.run_id is not None:
                        self.run_state.begin_finishing(self.run_state.run_id)
                if execution.outcome is not None and execution.outcome.kind == AgentRunOutcomeKind.INTERRUPTED:
                    execution.outcome = AgentRunOutcome(AgentRunOutcomeKind.STOPPED, result="子任务已停止")
                # 1.1. 一次性或已废弃的结果先留档；保留型在资源交还时交付正常报告。
                async with self.mutations.admission_lock, self.lock:
                    await self._ensure_result(execution)
                    notify = await self.identity.read_availability(definition.session_id, subagent_id)
                    if not definition.if_retain_context or not notify:
                        await self._commit_result(execution, notify=notify)
                    # 1.2. 一次性结束及父配置删除均废弃身份，普通保留型结束继续允许复用。
                    if not definition.if_retain_context or not self.identity.settings.exists(definition.session_id):
                        await self.subagents.definitions.retire(subagent_id)
                await self._publish_subagent(definition)
                # 2. 在受理锁和会话锁外等待实际清理，重试继续使用原执行和已保存结果。
                await execution.manager.cancel_pending_interrupt()
                await execution.manager.discard_pending_commands()
                record = await self.subagents.definitions.get(definition.session_id, subagent_id)
                if record.retired_at is not None and self.task_supervisor is not None:
                    await self.task_supervisor.stop_subagent_tasks(definition.session_id, subagent_id)
                if execution.agent is not None:
                    await execution.agent.close()
                # 2.1. destroy 可能在模型客户端关闭期间到达，结束前再清理其独立任务。
                async with session.lock:
                    record = await self.subagents.definitions.get(definition.session_id, subagent_id)
                if record.retired_at is not None and self.task_supervisor is not None:
                    await self.task_supervisor.stop_subagent_tasks(definition.session_id, subagent_id)
                retired_tasks_cleaned = record.retired_at is not None
                try:
                    await execution.manager.close()
                except Exception:
                    logger.exception("关闭子 Agent 状态连接失败：%s", subagent_id)
                # 3. 最后的短锁内完成资源交还；最后连接关闭失败也必须完成等待。
                async with self.mutations.admission_lock, session.lock:
                    record = await self.subagents.definitions.get(definition.session_id, subagent_id)
                    if record.retired_at is not None and not retired_tasks_cleaned:
                        continue
                    # 3.1. 保留型在资源交还时提交正常报告，父收到报告后即可再次委派。
                    if definition.if_retain_context:
                        await self._commit_result(execution,
                            notify=await self.identity.read_availability(definition.session_id, subagent_id))
                    session._idle()
                    session.subagent = None
                    session.activated = False
                    execution.cleanup_error = None
                    # 3.2. 使用锁内已读取的记录同步交还资源并发布身份状态。
                    payload = ({"subagent_id": subagent_id, "removed": True} if record.retired_at is not None else
                               {**asdict(self.snapshot(record.definition)), "removed": False})
                    self._emit(InteractionEvent(definition.session_id, InteractionEventKind.SUBAGENT_STATUS, payload))
                return
            except Exception as exc:
                execution.cleanup_error = str(exc)
                execution.finalizing = True
                logger.exception("子 Agent 收尾失败，将自动重试：%s", subagent_id)
                if self.runtime_phase == RuntimePhase.RUNNING:
                    await asyncio.sleep(AGENT_CLEANUP_RETRY_SECONDS)
                if self.runtime_phase != RuntimePhase.RUNNING:
                    # 4. 子历史使用独立表，由本次 Manager 补记退出正文；保存失败也释放实际资源。
                    try:
                        partial = session.shutdown_partial or ("", False, DESKTOP_CHANNEL, None)
                        await execution.manager.finalize_abandoned_turn(
                            partial=partial[0], partial_persisted=partial[1],
                            origin_channel=partial[2], partial_message_id=partial[3],
                        )
                    finally:
                        try:
                            if execution.agent is not None:
                                await execution.agent.close()
                        finally:
                            await execution.manager.close()
                    return


    async def accept_plugin(self, request: PluginRequest) -> None:
        """复用本上下文的来源核验，保存插件引导后再打断并唤醒。

        Args:
            request (PluginRequest): 已由运行入口绑定来源的具体请求。
        """
        session = self
        async with self.lock:
            # 1. 普通通知和界面提示属于会话；停止和引导必须仍指向原轮次。
            if request.kind in {PluginRequestKind.STOP_TURN, PluginRequestKind.STEER_TURN}:
                control = session.matching_control(request.run_id)
                if control is None:
                    logger.info("插件控制未受理：来源轮次已结束")
                    return
                if control.phase == TurnPhase.COMMITTING:
                    logger.info("插件控制未受理：来源轮次正在提交")
                    return
                if request.kind == PluginRequestKind.STOP_TURN:
                    session.request_interrupt(request.run_id, TurnInterruptReason.STOP)
                    return
            # 2. 引导正文保存成功后才打断，普通通知仅提醒 worker 检查持久输入。
            if request.kind in {PluginRequestKind.NOTIFY, PluginRequestKind.STEER_TURN}:
                manager = self._new_manager(request.session_id)
                try:
                    await manager.submit_plugin_notification(request.plugin_name,
                        request.name if request.kind == PluginRequestKind.NOTIFY else PluginRequestKind.STEER_TURN.value,
                        request.text)
                finally:
                    await manager.close()
                if request.kind == PluginRequestKind.STEER_TURN:
                    session.request_interrupt(request.run_id, TurnInterruptReason.STEER)
                self.wakeup.set()
            elif request.kind == PluginRequestKind.PUBLISH_EVENT:
                if request.name != InteractionEventKind.NOTICE or not isinstance(request.payload.get("text"), str):
                    raise ValueError("前端提示需要 notice 事件及 text 正文")
                self._emit(InteractionEvent(request.session_id, InteractionEventKind.NOTICE,
                    {"text": request.payload["text"], "plugin_name": request.plugin_name}, task_id=request.source_task_id))

    async def finish_execution_cleanup(self) -> None:
        """父删除或应用退出时等待原执行，继续释放会话保留的失败实例句柄。"""
        self.request_shutdown()
        if self.worker is not None:
            results = await asyncio.gather(self.worker, return_exceptions=True)
            for result in results:
                if isinstance(result, Exception):
                    raise result
        for agent in tuple(self.cleanup_agents):
            await agent.finish_pending_response()
            await agent.close()
            self.cleanup_agents.remove(agent)
        self._idle()
        self.resume_request_id = None
