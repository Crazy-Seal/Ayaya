"""子会话统一接受父消息，负责保留政策、最终父报告和持久销毁。"""
from app.schemas.runtime_types import CommandReceipt

import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
import logging
from uuid import uuid4

from app.agent.runtime.agent_execution import AgentExecution
from app.agent.runtime.requests import RuntimeRequest, SessionClosed
from app.agent.runtime.tasks.task_supervisor import TaskSupervisor
from app.agent.state.manager import SessionStateManager
from app.interaction.interaction_contract import ChannelCommand, InteractionEvent
from app.schemas.runtime_types import AgentRunOutcome, AgentRunOutcomeKind, ApprovalDecisionKind, ApprovalWait, CommandStatus, InboxKind, InteractionEventKind, ParentMessageWait, SessionState
from app.schemas.subagent_types import SubagentDefinition, SubagentInteractionKind, SubagentPublicStatus, SubagentSummary
from app.services.subagent_service import SubagentService


logger = logging.getLogger(__name__)


class SubagentSession:
    """拥有子上下文的政策和一个工作循环，普通任务由应用 Supervisor 独立持有。

    Attributes:
        definition (SubagentDefinition): 固定子定义、父归属和保留选项。
        _session_state (SessionState): 子输入资格的唯一来源。
        _execution (AgentExecution): 本子上下文独立的公共执行对象。
        _manager (SessionStateManager): 本会话拥有的子历史和输入对象。
        _result_ref (str | None): 本次工作最近的持久结果编号，与已有 run_id 相同。
    """

    def __init__(self, definition: SubagentDefinition, *, manager: SessionStateManager,
                 execution: AgentExecution, events: asyncio.Queue[RuntimeRequest],
                 store: SubagentService, task_supervisor: TaskSupervisor) -> None:
        """接收工厂创建的独立子对象，身份配置保持静态数据。

        Args:
            definition (SubagentDefinition): 已保存的子定义。
            manager (SessionStateManager): 由本会话关闭的状态对象。
            execution (AgentExecution): 借用同一 Manager 的公共执行机制。
            events (asyncio.Queue[RuntimeRequest]): 应用唯一运行队列。
            store (SubagentService): 子定义、交互和报告的持久业务。
            task_supervisor (TaskSupervisor): 应用共享的普通任务管理器。
        """
        self.definition, self.context_id = definition, definition.subagent_id
        self._manager, self._execution, self._events = manager, execution, events
        self._store, self._task_supervisor = store, task_supervisor
        self._session_state = SessionState.PAUSED
        self._lock, self._wakeup = asyncio.Lock(), asyncio.Event()
        self._worker: asyncio.Task[None] | None = None
        self._resume_requests: list[str] = []
        self._result_ref: str | None = None
        self._last_explicit_input = 0
        self._failed_input_ids: set[int] = set()
        self._started_at: str | None = None
        self._error: str | None = None
        self._closing_task: asyncio.Task[None] | None = None

    def snapshot(self) -> dict[str, object]:
        """只读复制本会话政策及执行展示数据。"""
        return {"session_state": self._session_state, **self._execution.presentation()}

    def summary(self) -> SubagentSummary:
        """将执行的只读等待数据投影为子面板摘要。"""
        execution = self._execution.snapshot()
        status = (SubagentPublicStatus.WAITING_PARENT if isinstance(execution.wait, ParentMessageWait) else
                  SubagentPublicStatus.RUNNING if execution.run_id is not None else SubagentPublicStatus.IDLE)
        return SubagentSummary(self.context_id, self.definition.name, self.definition.if_retain_context, status,
            self._started_at, self.definition.created_at.isoformat(), isinstance(execution.wait, ApprovalWait), self._error)

    def matches_run(self, run_id: str) -> bool:
        """关闭后拒绝命令来源，其他情况交给本执行核验编号。

        Args:
            run_id (str): 显式请求绑定的来源轮次。
        """
        return self._session_state != SessionState.CLOSED and self._execution.matches_run(run_id)

    def _publish(self, *, removed: bool = False) -> None:
        """发布已确定的子会话摘要，输出由 Scheduler 消费后交 Hub 编号。

        Args:
            removed (bool): 持久废弃已经成功时移除面板条目。
        """
        payload = {"subagent_id": self.context_id, "removed": True} if removed else {**asdict(self.summary()), "removed": False}
        self._events.put_nowait(InteractionEvent(self.definition.session_id, InteractionEventKind.SUBAGENT_STATUS, payload))

    def _wake_worker(self) -> None:
        """保存消息后提醒原 worker，前一工作已交还时建立下一次工作循环。"""
        self._wakeup.set()
        if self._session_state != SessionState.CLOSED and (self._worker is None or self._worker.done()):
            self._worker = asyncio.create_task(self._run_worker())

    async def activate_created(self, *, created: bool) -> CommandReceipt:
        """首次创建或创建请求重放时，只为仍待交付的初始输入安排执行。

        Args:
            created (bool): 本次事务是否首次写入定义和初始输入。

        Returns:
            CommandReceipt: 已保存定义的固定子编号。
        """
        async with self._lock:
            record = await self._store.definitions.get(self.definition.session_id, self.context_id)
            if record is None or record.retired_at is not None:
                return CommandReceipt(CommandStatus.CLOSED, self.context_id)
            batch = () if created else await self._manager.read_activation_batch(kind=InboxKind.AGENT_COMMAND)
            if created or batch:
                self._last_explicit_input = max((item.id for item in batch), default=0)
                self._session_state = SessionState.ENABLED
                self._wake_worker()
            self._publish()
            return CommandReceipt(CommandStatus.ACCEPTED, self.context_id)

    async def accept_message(self, text: str, source_id: str) -> CommandReceipt:
        """统一保存父消息，再请求本执行引导；回执只根据输入保存结果产生。

        Args:
            text (str): 父的新明确指令或回复。
            source_id (str): 固定调用来源，用于持久去重。

        Returns:
            CommandReceipt: 输入已保存、身份关闭或最终保存错误。
        """
        if not isinstance(text, str) or not text.strip():
            return CommandReceipt(CommandStatus.INVALID, self.context_id, detail="消息正文不能为空")
        async with self._lock:
            if self._session_state == SessionState.CLOSED:
                return CommandReceipt(CommandStatus.CLOSED, self.context_id)
            try:
                _, created, input_id = await self._store.definitions.append_message(self.definition, SubagentInteractionKind.TASK, text, source_id,
                    discarded_ids=tuple(self._failed_input_ids))
            except Exception as exc:
                return CommandReceipt(CommandStatus.FAILED, self.context_id, detail=str(exc))
            if created:
                self._failed_input_ids.clear()
                assert input_id is not None
                self._last_explicit_input = input_id
                self._session_state = SessionState.ENABLED
                self._error = None
                self._execution.request_steer(source_id)
                self._wake_worker()
                self._publish()
            return CommandReceipt(CommandStatus.ACCEPTED, self.context_id)

    def notify_inbox_changed(self) -> None:
        """普通通知提醒当前工作读取，空闲保留型子继续等待明确父消息。"""
        if self._session_state == SessionState.ENABLED and self._worker is not None:
            self._wakeup.set()

    def accepts_notifications(self) -> bool:
        """由会话政策核验迟到任务通知是否仍可交付。

        Returns:
            bool: 本进程会话受理入口仍然开放时为 True。
        """
        return self._session_state != SessionState.CLOSED

    async def stop_current(self, run_id: str | None = None) -> CommandReceipt:
        """停止本次子工作，保留型暂停，一次性按持久关闭政策结束。

        Args:
            run_id (str | None): 可选的固定目标执行编号。

        Returns:
            CommandReceipt: 此次停止控制的受理结果。
        """
        async with self._lock:
            if self._session_state == SessionState.CLOSED:
                return CommandReceipt(CommandStatus.CLOSED, self.context_id)
            receipt = self._execution.request_stop(run_id)
            if receipt.status == CommandStatus.STALE and run_id is None:
                receipt = CommandReceipt(CommandStatus.ACCEPTED, self.context_id)
            if receipt.accepted:
                self._session_state = SessionState.PAUSED
                self._wakeup.set()
            return receipt

    async def resolve_authorization(self, command: ChannelCommand) -> CommandReceipt:
        """保存首个有效审批决定，原 worker 顺序恢复审批或交付新指令。

        Args:
            command (ChannelCommand): 实际子上下文的审批决定。

        Returns:
            CommandReceipt: 决定的持久受理结果。
        """
        async with self._lock:
            if self._session_state == SessionState.CLOSED:
                return CommandReceipt(CommandStatus.CLOSED, self.context_id)
            receipt = await self._execution.resolve_authorization(command)
            if not receipt.accepted:
                return receipt
            if command.payload["kind"] == ApprovalDecisionKind.STEER:
                self._session_state = SessionState.ENABLED
            else:
                assert command.request_id is not None
                self._resume_requests.append(command.request_id)
            self._wake_worker()
            return receipt

    async def _finish_work(self, outcome: AgentRunOutcome) -> None:
        """交付最终父报告，按废弃事务结果关闭身份或保留暂停会话。

        Args:
            outcome (AgentRunOutcome): 公共执行已经确定的本次结果。
        """
        if outcome.kind in {AgentRunOutcomeKind.SAVE_FAILED, AgentRunOutcomeKind.FAILED}:
            self._error = outcome.error
        # 1. 保存本次结果并交付父报告，交付失败时保留子历史并通知界面。
        try:
            if self._result_ref is None:
                self._result_ref = uuid4().hex
                await self._store.results.start_subagent_run(self.definition, self._result_ref, None)
            if self._result_ref is not None:
                notify = await self._store.identity.read_availability(self.definition.session_id, self.context_id)
                if await self._store.finish_run(self._result_ref, outcome, notify=notify) and notify:
                    record = await self._store.results.get(self._result_ref)
                    if record is not None:
                        self._events.put_nowait(record)
        except Exception as exc:
            # 1.1. 下一次明确输入开启新的工作，本次交付错误随子摘要展示。
            self._error = f"结果交付失败：{exc}"
            self._events.put_nowait(InteractionEvent(self.context_id, InteractionEventKind.ERROR,
                {"detail": self._error, "report_failed": True}, run_id=outcome.run_id))
        # 2. 一次性身份在废弃提交成功后关闭，失败时保留暂停会话供后续明确操作。
        if not self.definition.if_retain_context:
            try:
                await self._store.definitions.retire(self.context_id)
            except Exception as exc:
                if self._session_state != SessionState.CLOSED:
                    self._session_state = SessionState.PAUSED
                self._error = f"一次性子上下文废弃失败：{exc}"
                self._events.put_nowait(InteractionEvent(self.context_id, InteractionEventKind.ERROR,
                    {"detail": self._error}, run_id=outcome.run_id))
            else:
                self._session_state = SessionState.CLOSED
        elif self._session_state != SessionState.CLOSED:
            self._session_state = SessionState.PAUSED
        self._result_ref = None
        self._publish(removed=self._session_state == SessionState.CLOSED)

    async def _prepare_run(self, run_id: str) -> bool:
        """在可停止的执行准备阶段保存本轮结果记录，保存后确认子会话仍可工作。

        Args:
            run_id (str): Execution 分配并已经接受控制的轮次。
        Returns:
            bool: 本会话仍允许执行时为 True。
        """
        if self._session_state != SessionState.ENABLED:
            return False
        self._started_at = datetime.now(timezone.utc).isoformat()
        await self._store.results.start_subagent_run(self.definition, run_id, self._result_ref)
        self._result_ref = run_id
        return self._session_state == SessionState.ENABLED

    async def _run_worker(self) -> None:
        """按输入身份顺序调用公共执行，正常结束与新消息受理共用本会话短锁。"""
        try:
            while self._session_state != SessionState.CLOSED:
                self._wakeup.clear()
                request_id = self._resume_requests.pop(0) if self._resume_requests else None
                await self._execution.finish_requested_stop()
                waiting = self._execution.is_waiting()
                if self._session_state == SessionState.PAUSED:
                    await self._execution.finish_waiting()
                    outcome = AgentRunOutcome(AgentRunOutcomeKind.STOPPED, run_id=self._result_ref)
                    async with self._lock:
                        # 1. 停止协议保存期间受理的新明确消息，继续由原 worker 交付。
                        if self._session_state == SessionState.ENABLED:
                            continue
                        await self._finish_work(outcome)
                    break
                batch = () if request_id else await self._manager.read_activation_batch(
                    kind=InboxKind.AGENT_COMMAND if waiting else None, include_queued=False)
                if self._session_state != SessionState.ENABLED:
                    async with self._lock:
                        if self._session_state == SessionState.ENABLED:
                            continue
                        await self._finish_work(AgentRunOutcome(AgentRunOutcomeKind.STOPPED, run_id=self._result_ref))
                    break
                if not batch and request_id is None:
                    await self._wakeup.wait()
                    continue
                handled_through = max((item.id for item in batch if item.kind == InboxKind.AGENT_COMMAND), default=self._last_explicit_input)
                if request_id:
                    outcome = await self._execution.resume(request_id)
                else:
                    outcome = await self._execution.execute(batch, prepare=self._prepare_run)
                self._publish()
                if outcome.kind == AgentRunOutcomeKind.INTERRUPTED:
                    continue
                async with self._lock:
                    if outcome.kind in {AgentRunOutcomeKind.SAVE_FAILED, AgentRunOutcomeKind.FAILED}:
                        self._failed_input_ids.update(item.id for item in batch if item.kind == InboxKind.AGENT_COMMAND)
                    # 1. 已先受理的新明确输入继续执行；失败则保持暂停等待下一次明确操作。
                    pending = await self._manager.read_activation_batch(kind=InboxKind.AGENT_COMMAND)
                    if outcome.kind not in {AgentRunOutcomeKind.FAILED, AgentRunOutcomeKind.SAVE_FAILED} and (
                            self._last_explicit_input > handled_through or outcome.kind == AgentRunOutcomeKind.STEERED) and pending:
                        continue
                    await self._finish_work(outcome)
                break
            # 2. 销毁可能唤醒正在等待父消息的 worker，原轮次在此保存停止协议和最终结果。
            if self._session_state == SessionState.CLOSED and self._result_ref is not None:
                await self._execution.finish_waiting()
                async with self._lock:
                    await self._finish_work(AgentRunOutcome(AgentRunOutcomeKind.STOPPED, run_id=self._result_ref))
        except Exception as exc:
            logger.exception("子会话工作失败：%s", self.context_id)
            self._error = str(exc)
            failure = await self._execution.handle_failure(exc)
            async with self._lock:
                if self._result_ref is not None:
                    await self._finish_work(failure)
                else:
                    if self._session_state != SessionState.CLOSED:
                        self._session_state = SessionState.PAUSED
                    self._publish()
        finally:
            # 1. 报告交付后释放本次 Agent，一次性后台任务随本次工作结束。
            await self._execution.close_current_agent()
            if not self.definition.if_retain_context or self._session_state == SessionState.CLOSED:
                await self._task_supervisor.stop_subagent_tasks(self.definition.session_id, self.context_id)
            # 2. 已关闭会话释放连接并注销，废弃失败的暂停会话保留原登记。
            if self._session_state == SessionState.CLOSED:
                await self._manager.close()
                self._events.put_nowait(SessionClosed(self.context_id, id(self)))
            self._worker = None
            if self._session_state == SessionState.ENABLED:
                self._wake_worker()

    async def destroy(self) -> CommandReceipt:
        """持久废弃成功后封闭受理并立即回执，资源关闭随后进行。

        Returns:
            CommandReceipt: 持久废弃成功或原始保存错误。
        """
        async with self._lock:
            try:
                await self._store.definitions.retire(self.context_id)
            except Exception as exc:
                return CommandReceipt(CommandStatus.FAILED, self.context_id, detail=str(exc))
            self.begin_close()
            self._publish(removed=True)
            if self._closing_task is None:
                self._closing_task = asyncio.create_task(self.close("持久销毁"))
            return CommandReceipt(CommandStatus.ACCEPTED, self.context_id)

    def begin_close(self) -> None:
        """封闭本会话受理并停止执行，应用退出保持原保留型定义可用。"""
        self._session_state = SessionState.CLOSED
        self._execution.begin_close()
        self._wakeup.set()

    async def close(self, reason: str) -> None:
        """结束进程内子会话及资源，一次性子按原政策废弃。

        Args:
            reason (str): 持久销毁、父删除或应用退出的原因。
        """
        current = asyncio.current_task()
        if self._closing_task is not None and self._closing_task is not current:
            await asyncio.shield(self._closing_task)
            return
        self.begin_close()
        if self._worker is not None:
            await asyncio.gather(self._worker, return_exceptions=True)
        await self._execution.close(reason)
        await self._task_supervisor.stop_subagent_tasks(self.definition.session_id, self.context_id)
        await self._manager.close()
        self._events.put_nowait(SessionClosed(self.context_id, id(self)))
