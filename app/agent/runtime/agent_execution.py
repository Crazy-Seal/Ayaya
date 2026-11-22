"""管理一个实际上下文的执行、引导、等待、故障恢复和当前 Agent 资源。"""

from __future__ import annotations
from app.schemas.runtime_types import CommandReceipt

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import asdict, replace
import logging
import sqlite3
from uuid import uuid4

from app.agent.agent import Agent
from app.agent.core.events import AgentEvent, EventType
from app.agent.runtime.agent_run_state_machine import AgentRunStateMachine
from app.agent.runtime.requests import ApprovalInvalidated, ExecutionEvent, RuntimeRequest
from app.agent.runtime.session_inbox import InboxEnvelope
from app.agent.runtime.turn_control import TurnControl
from app.agent.state.manager import RequiredSaveError, SessionStateManager
from app.crud.sqlite_save import SqliteRollbackError
from app.agent.utils.infra.bounded_operation import OperationTimeoutError
from app.interaction.interaction_contract import ChannelCommand, InteractionEvent
from app.schemas.runtime_types import AgentRunOutcome, AgentRunOutcomeKind, ApprovalDecisionKind, ApprovalWait, AuthorizationResolutionKind, CommandStatus, DESKTOP_CHANNEL, ExecutionPhase, ExecutionSnapshot, InboxKind, InteractionEventKind, SessionPhase, TurnInterruptReason


logger = logging.getLogger(__name__)


class AgentExecution:
    """父子会话共用执行机制，每个 context_id 持有自己的实例和唯一执行状态。

    Attributes:
        context_id (str): 本执行实际使用的历史和 Inbox 上下文。
        _state (AgentRunStateMachine): 阶段、轮次、等待和控制原因的唯一来源。
        _manager (SessionStateManager): 所属会话借用的持久状态对象。
        _agent (Agent | None): 本次工作按需创建、等待期间保留的实际执行资源。
        _active (asyncio.Task | None): 正在等待的本段 Agent 调用，关闭时可请求其取消。
    """

    def __init__(self, context_id: str, *, manager: SessionStateManager,
                 agent_factory: Callable[[SessionStateManager], Agent],
                 events: asyncio.Queue[RuntimeRequest]) -> None:
        """接收会话的持久化和装配能力，保持执行与父子政策独立。

        Args:
            context_id (str): 实际上下文编号。
            manager (SessionStateManager): 会话拥有的状态对象。
            agent_factory (Callable): 按需构造当前实际 Agent 的函数。
            events (asyncio.Queue[RuntimeRequest]): 应用统一请求及事件队列。
        """
        self.context_id = context_id
        self._manager, self._agent_factory, self._events = manager, agent_factory, events
        self._state = AgentRunStateMachine(context_id)
        self._agent: Agent | None = None
        self._control: TurnControl | None = None
        self._active: asyncio.Task[AgentRunOutcome] | None = None
        self._origin = DESKTOP_CHANNEL
        self._approval: dict[str, object] | None = None
        self._last_fault: dict[str, object] | None = None

    def snapshot(self) -> ExecutionSnapshot:
        """复制当前执行摘要，供会话展示和查询。"""
        return self._state.snapshot()

    def presentation(self) -> dict[str, object]:
        """从本执行事实生成界面摘要，保持快照查询为同步只读操作。

        Returns:
            dict[str, object]: 界面阶段、实际执行摘要、审批材料和最近保存故障。
        """
        snapshot = self.snapshot()
        phase = (SessionPhase.IDLE if snapshot.phase in {ExecutionPhase.IDLE, ExecutionPhase.FAULTED} else
                 SessionPhase.PENDING_INTERRUPT if isinstance(snapshot.wait, ApprovalWait) else SessionPhase.RUNNING)
        return {"phase": phase, "run_id": snapshot.run_id, "origin_channel": self._origin,
                "interrupt": self.approval(), "execution": asdict(snapshot), "save_failure": self._last_fault}

    def approval(self) -> dict[str, object] | None:
        """返回当前已保存审批的展示副本，停止或决定后自动失效。"""
        return dict(self._approval) if isinstance(self._state.wait, ApprovalWait) and self._approval else None

    def matches_run(self, run_id: str) -> bool:
        """核验命令来源是否仍属于本执行，用于显式 Agent 请求受理。

        Args:
            run_id (str): 请求绑定的来源执行编号。
        """
        return self._state.run_id == run_id

    def is_waiting(self) -> bool:
        """供会话选择可唤醒执行的输入，等待事实始终取自执行状态机。"""
        return self._state.phase == ExecutionPhase.WAITING

    async def finish_requested_stop(self) -> None:
        """工作循环被控制请求唤醒后，先完成已接受的等待停止。"""
        if self._state.phase == ExecutionPhase.STOPPING:
            await self.finish_waiting()

    def request_steer(self, expected_run_id: str | None = None) -> CommandReceipt:
        """用已经保存的明确输入引导当前执行，空闲或提交时交由会话后续处理。

        Args:
            expected_run_id (str | None): 显式引导的目标轮次。

        Returns:
            CommandReceipt: 此次控制是否被接受，与消息保存回执分别返回。
        """
        receipt = self._state.request_steer(expected_run_id)
        if receipt.accepted and self._control:
            self._control.wake_interrupt()
        return receipt

    def request_stop(self, expected_run_id: str | None = None) -> CommandReceipt:
        """请求停止匹配的本执行，已经开始的最终保存继续确认。

        Args:
            expected_run_id (str | None): 固定目标轮次，省略时使用当前轮次。

        Returns:
            CommandReceipt: 已接受、过期或最终保存太迟的结果。
        """
        receipt = self._state.request_stop(expected_run_id)
        if receipt.accepted and self._control:
            self._control.wake_interrupt()
        return receipt

    async def _emit_agent(self, event: AgentEvent) -> None:
        """将本轮事件连同固定执行身份投递，审批材料只用于展示。

        Args:
            event (AgentEvent): Agent 已确定的执行事件。
        """
        run_id = self._state.run_id
        if run_id is None:
            raise RuntimeError("空闲执行不能发布本轮事件")
        if event.type == EventType.RUN_STARTED:
            self._origin = str(event.data["origin_channel"])
        elif event.type == EventType.INTERRUPT and isinstance(self._state.wait, ApprovalWait):
            self._approval = dict(event.data)
        self._events.put_nowait(ExecutionEvent(self.context_id, run_id, self._origin, event))

    async def resolve_authorization(self, command: ChannelCommand) -> CommandReceipt:
        """核验当前审批并保存首个有效决定，父子会话随后按自己的政策安排工作。

        Args:
            command (ChannelCommand): 审批编号、决定内容和操作渠道。

        Returns:
            CommandReceipt: 决定已保存、请求过期、参数错误或最终保存失败。
        """
        decision = command.payload
        if not command.request_id or decision.get("kind") not in set(ApprovalDecisionKind):
            return CommandReceipt(CommandStatus.INVALID, self.context_id, detail="审批编号或决定无效")
        if decision["kind"] == ApprovalDecisionKind.STEER and not str(decision.get("message") or "").strip():
            return CommandReceipt(CommandStatus.INVALID, self.context_id, detail="引导消息不能为空")
        wait = self._state.wait
        if not isinstance(wait, ApprovalWait) or wait.request_id != command.request_id:
            return CommandReceipt(CommandStatus.STALE, self.context_id)
        run_id = self._state.run_id
        if command.target_run_id is not None and command.target_run_id != run_id:
            return CommandReceipt(CommandStatus.STALE, self.context_id)
        try:
            origin = await self._manager.record_authorization_decision(command.request_id, decision, command.origin_channel)
        except Exception as exc:
            return CommandReceipt(CommandStatus.FAILED, self.context_id, run_id, detail=str(exc))
        if origin is None:
            return CommandReceipt(CommandStatus.STALE, self.context_id, run_id)
        self._approval = None
        self._events.put_nowait(InteractionEvent(self.context_id, InteractionEventKind.AUTHORIZATION_RESOLVED,
            {"outcome": AuthorizationResolutionKind(decision["kind"]), "actor_channel": command.origin_channel},
            origin_channel=origin, request_id=command.request_id, run_id=run_id))
        if decision["kind"] == ApprovalDecisionKind.STEER:
            self.request_steer(expected_run_id=run_id)
        return CommandReceipt(CommandStatus.ACCEPTED, self.context_id, run_id)

    def check_queue_target(self) -> CommandReceipt:
        """核验当前执行能否承接显式排队，消息保存仍由所属会话完成。

        Returns:
            CommandReceipt: 活动执行允许排队，空闲或最终提交时明确拒绝。
        """
        snapshot = self.snapshot()
        status = (CommandStatus.ACCEPTED if snapshot.phase in {
            ExecutionPhase.RUNNING, ExecutionPhase.WAITING, ExecutionPhase.STOPPING} else
            CommandStatus.TOO_LATE if snapshot.phase == ExecutionPhase.COMMITTING else CommandStatus.STALE)
        return CommandReceipt(status, self.context_id, snapshot.run_id, detail=None if status == CommandStatus.ACCEPTED else "当前没有可排队的执行")

    async def execute(self, batch: tuple[InboxEnvelope, ...], origin_channel: str = DESKTOP_CHANNEL,
                      *, prepare: Callable[[str], Awaitable[bool]]) -> AgentRunOutcome:
        """处理已受理批次，先结束被新输入替代的等待，再开始新轮。

        Args:
            batch (tuple[InboxEnvelope, ...]): 会话按政策选定的待交付输入。
            origin_channel (str): 没有真人输入时的默认渠道。
            prepare (Callable): 会话准备本轮业务记录，并确认输入仍可开始执行。

        Returns:
            AgentRunOutcome: 明确结果及其原执行编号。
        """
        if self._state.phase == ExecutionPhase.FAULTED:
            if not await self.restore_committed_state():
                return AgentRunOutcome(AgentRunOutcomeKind.SAVE_FAILED, error="恢复已提交上下文失败")
        try:
            # 1. 等待中的旧工具在原执行中结束，新输入始终使用新的 run_id。
            if self._state.phase in {ExecutionPhase.WAITING, ExecutionPhase.STOPPING}:
                await self.finish_waiting()
            next_run_id = uuid4().hex
            self._state.begin_run(next_run_id)
            self._origin = origin_channel
            self._control = TurnControl(next_run_id, self._state)
            # 2. 业务记录保存属于本轮准备；期间停止仍由同一执行状态机接受。
            permitted = await prepare(next_run_id)
            if not permitted:
                self.request_stop(next_run_id)
            if self._state.phase == ExecutionPhase.STOPPING:
                reason = await self.finish_waiting()
                kind = AgentRunOutcomeKind.STEERED if reason == TurnInterruptReason.STEER else AgentRunOutcomeKind.STOPPED
                return AgentRunOutcome(kind, context_id=self.context_id, run_id=next_run_id)
            if self._agent is None:
                self._agent = self._agent_factory(self._manager)
            human = next((item for item in batch if item.kind == InboxKind.HUMAN), None)
            if human is not None:
                operation = self._agent.run(human.payload["message"], images=human.payload.get("images"),
                    origin_channel=human.origin_channel, inbox_input=human, inbox_inputs=batch,
                    turn_control=self._control, receive_task_notifications=True, emit=self._emit_agent)
            else:
                operation = self._agent.run_runtime_inputs(inbox_inputs=batch, turn_control=self._control,
                    receive_task_notifications=True, emit=self._emit_agent)
            return await self._advance(operation)
        except Exception as exc:
            return await self.handle_failure(exc)

    async def resume(self, request_id: str) -> AgentRunOutcome:
        """恢复已经保存决定的原审批，保持 run_id 和实际 Agent。

        Args:
            request_id (str): 首次有效审批决定对应的请求。

        Returns:
            AgentRunOutcome: 原轮次推进后的结果。
        """
        run_id = self._state.run_id
        if run_id is None or not self._state.resume_approval(run_id, request_id).accepted:
            return AgentRunOutcome(AgentRunOutcomeKind.INTERRUPTED, run_id=run_id, wait=self._state.wait)
        self._approval = None
        assert self._agent is not None and self._control is not None
        return await self._advance(self._agent.resume(request_id, turn_control=self._control,
            receive_task_notifications=True, emit=self._emit_agent))

    async def _advance(self, operation: Awaitable[AgentRunOutcome]) -> AgentRunOutcome:
        """等待一次实际执行，通过返回结果推进阶段，业务和保存均不在此重试。

        Args:
            operation (Awaitable[AgentRunOutcome]): 当前实际 Agent 的一次调用。

        Returns:
            AgentRunOutcome: 包含实际上下文和轮次的结果。
        """
        run_id = self._state.run_id
        assert run_id is not None
        async def invoke() -> AgentRunOutcome:
            """运行一次已准备的 Agent 调用，句柄仅用于关闭时请求中断。"""
            return await operation
        self._active = asyncio.create_task(invoke())
        try:
            outcome = await self._active
        except asyncio.CancelledError:
            # 1. 关闭可能发生在 Agent 尚未开始时，由原执行补齐停止协议。
            await self.finish_waiting()
            return AgentRunOutcome(AgentRunOutcomeKind.STOPPED, context_id=self.context_id, run_id=run_id)
        except Exception as exc:
            return await self.handle_failure(exc)
        finally:
            self._active = None
        outcome = replace(outcome, context_id=self.context_id, run_id=run_id)
        if outcome.kind == AgentRunOutcomeKind.SAVE_FAILED:
            return await self._fault(RequiredSaveError(outcome.error or "执行结果保存失败"))
        if outcome.kind == AgentRunOutcomeKind.INTERRUPTED:
            return outcome
        if outcome.kind == AgentRunOutcomeKind.FAILED:
            self._events.put_nowait(InteractionEvent(self.context_id, InteractionEventKind.ERROR,
                {"detail": outcome.error}, origin_channel=self._origin, run_id=run_id))
        # 2. Agent 已交还结束结果；资源关闭在结果交付之后另行发生。
        if self._state.phase != ExecutionPhase.COMMITTING:
            self._state.begin_commit(run_id)
        self._state.commit_succeeded(run_id)
        self._control, self._approval = None, None
        return outcome

    async def finish_waiting(self) -> TurnInterruptReason | None:
        """停止或新明确输入替代等待时，保存旧协议并交还实际结束原因。

        Returns:
            TurnInterruptReason | None: 本次保存采用的停止或引导原因；没有待结束轮次时为空。
        """
        run_id = self._state.run_id
        if run_id is None or self._state.phase not in {ExecutionPhase.WAITING, ExecutionPhase.STOPPING}:
            return
        if self._state.phase == ExecutionPhase.WAITING:
            self.request_stop()
        cancelled = await self._manager.cancel_pending_interrupt()
        if cancelled is not None:
            self._events.put_nowait(InteractionEvent(self.context_id, InteractionEventKind.AUTHORIZATION_RESOLVED,
                {"outcome": AuthorizationResolutionKind.CANCELLED}, origin_channel=cancelled[1], request_id=cancelled[0]))
        if self._state.phase != ExecutionPhase.COMMITTING:
            self._state.begin_commit(run_id)
        # 1. 审批取消期间仍可接受停止，最终保存采用进入提交阶段时的控制原因。
        reason = self._state.reason
        assert reason is not None
        state = await self._manager.load()
        control = self._control
        await self._manager.finish_interrupted_turn(state, reason=reason,
            partial=control.partial if control else "",
            partial_persisted=control.partial_persisted if control else False, origin_channel=self._origin)
        self._state.commit_succeeded(run_id)
        self._events.put_nowait(InteractionEvent(self.context_id, InteractionEventKind.RUN_COMPLETED,
            {"value": {"interrupted": True}}, origin_channel=self._origin, run_id=run_id))
        self._control, self._approval = None, None
        return reason

    async def handle_failure(self, exc: Exception) -> AgentRunOutcome:
        """把会话准备或执行错误交到本执行的最终结果出口。

        Args:
            exc (Exception): 所属持久操作或实际执行返回的最终错误。

        Returns:
            AgentRunOutcome: 保存故障进入恢复流程，其他业务错误结束当前轮次。
        """
        if isinstance(exc, (RequiredSaveError, sqlite3.Error, OSError, SqliteRollbackError, OperationTimeoutError)):
            return await self._fault(exc)
        logger.error("执行失败：%s", self.context_id, exc_info=exc)
        run_id = self._state.run_id
        try:
            if self._control is not None:
                self._control.begin_failed_commit()
            state = await self._manager.load()
            await self._manager.finish_failed_turn(state, error=str(exc), origin_channel=self._origin)
        except Exception as save_error:
            return await self._fault(save_error)
        self._events.put_nowait(InteractionEvent(self.context_id, InteractionEventKind.ERROR,
            {"detail": str(exc)}, origin_channel=self._origin, run_id=run_id))
        if run_id is not None and self._state.phase != ExecutionPhase.FAULTED:
            if self._state.phase != ExecutionPhase.COMMITTING:
                self._state.begin_commit(run_id)
            self._state.commit_succeeded(run_id)
            self._control, self._approval = None, None
        return AgentRunOutcome(AgentRunOutcomeKind.FAILED, error=str(exc), context_id=self.context_id, run_id=run_id)

    async def _fault(self, exc: Exception) -> AgentRunOutcome:
        """必需保存最终失败后报告故障，并尝试读取最后已提交上下文。

        Args:
            exc (Exception): DAO 或执行准备返回的最终存储原因。

        Returns:
            AgentRunOutcome: 会话据此暂停的保存失败结果。
        """
        run_id = self._state.run_id
        if run_id is None:
            run_id = uuid4().hex
            self._state.begin_run(run_id)
        if self._state.phase != ExecutionPhase.FAULTED:
            self._state.commit_failed(run_id, str(exc))
        self._events.put_nowait(ApprovalInvalidated(self.context_id))
        self._last_fault = {"run_id": run_id, "detail": str(exc), "restored": False}
        self._events.put_nowait(InteractionEvent(self.context_id, InteractionEventKind.ERROR,
            {**self._last_fault, "save_failed": True}, origin_channel=self._origin, run_id=run_id))
        await self.restore_committed_state()
        return AgentRunOutcome(AgentRunOutcomeKind.SAVE_FAILED, error=str(exc), context_id=self.context_id, run_id=run_id)

    async def restore_committed_state(self) -> bool:
        """读取最后成功保存的位置，成功后发布历史刷新，失败时保留 FAULTED。

        Returns:
            bool: 已提交上下文读取成功并可接受后续明确输入时为 True。
        """
        snapshot = self.snapshot()
        if snapshot.phase != ExecutionPhase.FAULTED:
            return True
        assert snapshot.run_id is not None
        try:
            state = await self._manager.load()
            position = await self._manager.committed_history_position()
        except Exception:
            logger.exception("恢复已提交上下文失败：%s", self.context_id)
            return False
        self._state.restore_succeeded(snapshot.run_id)
        self._control, self._approval = None, None
        self._last_fault = {**(self._last_fault or {}), "restored": True, "through_message_id": position}
        self._events.put_nowait(InteractionEvent(self.context_id, InteractionEventKind.HISTORY_RESTORED,
            {**self._last_fault, "execution": asdict(self.snapshot())}, origin_channel=self._origin, run_id=snapshot.run_id))
        return True

    async def close_current_agent(self) -> None:
        """结果交付后关闭当前实际 Agent，保留本执行对象及借用的会话连接。"""
        agent, self._agent = self._agent, None
        if agent is not None:
            await agent.close()

    def begin_close(self) -> None:
        """会话关闭入口先接受停止，再取消尚未进入最终保存的实际调用。"""
        self.request_stop()
        if self._active is not None and self._state.phase != ExecutionPhase.COMMITTING:
            self._active.cancel()

    async def close(self, reason: str) -> None:
        """结束本会话的执行和当前 Agent，借用连接随后由所属会话关闭。

        Args:
            reason (str): 应用退出或会话关闭的业务原因。
        """
        self.begin_close()
        if self._active is not None:
            await asyncio.gather(self._active, return_exceptions=True)
        if self._state.phase in {ExecutionPhase.WAITING, ExecutionPhase.STOPPING}:
            try:
                await self.finish_waiting()
            except Exception as exc:
                await self._fault(exc)
        await self.close_current_agent()
