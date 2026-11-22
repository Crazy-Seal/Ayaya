"""父会话受理真人和插件输入，决定排队、通知以及停止后的暂停政策。"""
from app.schemas.runtime_types import CommandReceipt

import asyncio
import logging

from app.agent.runtime.agent_execution import AgentExecution
from app.agent.runtime.requests import RuntimeRequest, SessionClosed
from app.agent.state.manager import SessionStateManager
from app.interaction.interaction_contract import InteractionCommand, InteractionEvent
from app.schemas.runtime_types import AgentRunOutcomeKind, ApprovalDecisionKind, CommandKind, CommandStatus, InboxKind, InteractionEventKind, SessionState
from app.schemas.task_types import PluginRequest, PluginRequestKind


logger = logging.getLogger(__name__)


class ParentSession:
    """管理父上下文自己的输入政策，耗时执行由一个 worker 顺序调用。

    Attributes:
        context_id (str): 父配置和对话上下文编号。
        _session_state (SessionState): 本会话执行资格的唯一来源。
        _manager (SessionStateManager): 会话拥有的输入及历史对象。
        _execution (AgentExecution): 本上下文独立的执行机制。
        _lock (asyncio.Lock): 排序输入受理、停止和执行结束政策。
    """

    def __init__(self, context_id: str, *, manager: SessionStateManager,
                 execution: AgentExecution, events: asyncio.Queue[RuntimeRequest]) -> None:
        """接收应用工厂装配的会话依赖，初始保持暂停。

        Args:
            context_id (str): 本父上下文编号。
            manager (SessionStateManager): 本会话拥有并关闭的状态对象。
            execution (AgentExecution): 借用同一 Manager 的独立执行对象。
            events (asyncio.Queue[RuntimeRequest]): 统一运行队列。
        """
        self.context_id = context_id
        self._manager, self._execution, self._events = manager, execution, events
        self._session_state = SessionState.PAUSED
        self._lock, self._wakeup = asyncio.Lock(), asyncio.Event()
        self._worker: asyncio.Task[None] | None = None
        self._resume_requests: list[str] = []
        self._failed_input_ids: set[int] = set()

    def snapshot(self) -> dict[str, object]:
        """复制本会话政策及执行对象提供的展示摘要。"""
        return {"session_state": self._session_state, **self._execution.presentation()}

    def matches_run(self, run_id: str) -> bool:
        """让命令入口核验来源轮次，关闭后拒绝新的显式请求。

        Args:
            run_id (str): 显式 Agent 请求绑定的原轮次。
        """
        return self._session_state != SessionState.CLOSED and self._execution.matches_run(run_id)

    def _wake_worker(self) -> None:
        """唤醒已存在的 worker，首次明确输入时建立本会话唯一工作循环。"""
        if self._session_state == SessionState.CLOSED:
            return
        self._wakeup.set()
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run_worker())

    def notify_inbox_changed(self) -> None:
        """普通通知只在父会话已经启用时安排工作，暂停时继续留在 Inbox。"""
        if self._session_state == SessionState.ENABLED:
            self._wake_worker()

    async def accept_human(self, command: InteractionCommand) -> CommandReceipt:
        """先保存真人输入，再恢复本会话工作资格并请求允许的引导。

        Args:
            command (InteractionCommand): 普通真人消息、固定轮次引导或显式排队。

        Returns:
            CommandReceipt: 按输入保存结果产生的受理回执。
        """
        text, images = command.payload.get("message"), command.payload.get("images")
        if not isinstance(text, str) or (not text.strip() and not images):
            return CommandReceipt(CommandStatus.INVALID, self.context_id, detail="消息不能为空")
        async with self._lock:
            if self._session_state == SessionState.CLOSED:
                return CommandReceipt(CommandStatus.CLOSED, self.context_id)
            if command.kind == CommandKind.HUMAN_STEER and (
                    not command.target_run_id or not self._execution.matches_run(command.target_run_id)):
                return CommandReceipt(CommandStatus.STALE, self.context_id, detail="没有匹配的活动轮次")
            queued = command.kind == CommandKind.QUEUE_TURN
            if queued:
                if self._session_state != SessionState.ENABLED:
                    return CommandReceipt(CommandStatus.STALE, self.context_id, detail="当前会话没有可排队的执行")
                permission = self._execution.check_queue_target()
                if not permission.accepted:
                    return permission
            try:
                envelope = await self._manager.submit_human(text, images=images, queued=queued,
                    discarded_ids=tuple(self._failed_input_ids))
            except Exception as exc:
                return CommandReceipt(CommandStatus.FAILED, self.context_id, detail=str(exc))
            self._failed_input_ids.clear()
            self._session_state = SessionState.ENABLED
            if queued:
                self._events.put_nowait(InteractionEvent(self.context_id, InteractionEventKind.QUEUED_MESSAGE,
                    {"input_sequence": envelope.id, "message": text, "images": images or []}))
            else:
                self._execution.request_steer(expected_run_id=command.target_run_id)
            self._wake_worker()
            return CommandReceipt(CommandStatus.ACCEPTED, self.context_id)

    async def cancel_queued(self, command: InteractionCommand) -> CommandReceipt:
        """撤销本会话尚未交付的排队输入，保存成功后发布移除事件。

        Args:
            command (InteractionCommand): 携带 input_sequence 的撤销请求。

        Returns:
            CommandReceipt: 实际撤销或已交付的结果。
        """
        identifier = command.payload.get("input_sequence")
        if not isinstance(identifier, int):
            return CommandReceipt(CommandStatus.INVALID, self.context_id, detail="缺少排队消息编号")
        async with self._lock:
            if self._session_state == SessionState.CLOSED:
                return CommandReceipt(CommandStatus.CLOSED, self.context_id)
            try:
                changed = await self._manager.cancel_queued(identifier)
            except Exception as exc:
                return CommandReceipt(CommandStatus.FAILED, self.context_id, detail=str(exc))
            if changed:
                self._events.put_nowait(InteractionEvent(self.context_id, InteractionEventKind.QUEUED_CANCELLED,
                    {"input_sequence": identifier}))
            return CommandReceipt(CommandStatus.ACCEPTED if changed else CommandStatus.STALE, self.context_id)

    async def stop_current(self, run_id: str | None = None) -> CommandReceipt:
        """停止指定执行，或暂停已启用父会话的当前工作。

        无目标轮次时也可暂停输入选择和执行间隙；子执行和独立后台任务继续运行。

        Args:
            run_id (str | None): 固定的目标轮次。

        Returns:
            CommandReceipt: 执行停止或会话暂停已接受，过期轮次及暂停空会话返回 STALE。
        """
        async with self._lock:
            if self._session_state == SessionState.CLOSED:
                return CommandReceipt(CommandStatus.CLOSED, self.context_id)
            receipt = self._execution.request_stop(run_id)
            if receipt.status == CommandStatus.STALE and run_id is None and self._session_state == SessionState.ENABLED:
                receipt = CommandReceipt(CommandStatus.ACCEPTED, self.context_id)
            if receipt.accepted:
                self._session_state = SessionState.PAUSED
                self._wake_worker()
            return receipt

    async def resolve_authorization(self, command: InteractionCommand) -> CommandReceipt:
        """保存有效的首次审批决定，再安排原轮恢复或处理新明确输入。

        Args:
            command (InteractionCommand): 匹配请求编号的审批决定。

        Returns:
            CommandReceipt: 决定已保存、过期或存储失败。
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

    async def accept_plugin(self, request: PluginRequest) -> CommandReceipt:
        """保存插件内容，按激活意图恢复会话资格并处理当前执行的引导。

        Args:
            request (PluginRequest): 框架已经绑定来源的插件请求。

        Returns:
            CommandReceipt: 本次通知或控制的最终受理结果。
        """
        async with self._lock:
            if self._session_state == SessionState.CLOSED:
                return CommandReceipt(CommandStatus.CLOSED, self.context_id)
            if request.kind == PluginRequestKind.STOP_TURN:
                receipt = self._execution.request_stop()
                if receipt.accepted:
                    self._wake_worker()
                return receipt
            if request.kind == PluginRequestKind.PUBLISH_EVENT:
                # 1. 仅接收框架支持的展示字段，事件来源使用已绑定的插件调用对象。
                text = request.payload.get("text")
                payload: dict[str, object]
                if request.name == InteractionEventKind.NOTICE and isinstance(text, str):
                    payload = {"text": text, "plugin_name": request.plugin_name}
                elif request.name == InteractionEventKind.STATUS_NOTICE:
                    notice_id, active = request.payload.get("id"), request.payload.get("active")
                    if not isinstance(notice_id, str) or not notice_id.strip() or not isinstance(active, bool):
                        return CommandReceipt(CommandStatus.INVALID, self.context_id)
                    if active and (not isinstance(text, str) or not text.strip()):
                        return CommandReceipt(CommandStatus.INVALID, self.context_id)
                    payload = {"id": notice_id, "active": active, "plugin_name": request.plugin_name}
                    if active:
                        payload["text"] = text
                else:
                    return CommandReceipt(CommandStatus.INVALID, self.context_id)
                # 2. 持续提示随插件业务更新和显式关闭，任务编号用于标识发布来源。
                self._events.put_nowait(InteractionEvent(self.context_id, request.name, payload,
                    task_id=request.source_task_id))
                return CommandReceipt(CommandStatus.ACCEPTED, self.context_id)
            try:
                name = PluginRequestKind.STEER_TURN.value if request.kind == PluginRequestKind.STEER_TURN else request.name
                await self._manager.submit_plugin_notification(request.plugin_name, name, request.text)
            except Exception as exc:
                return CommandReceipt(CommandStatus.FAILED, self.context_id, detail=str(exc))
            # 3. 新通知保存成功后应用激活意图，worker 按会话现有规则交付输入。
            if request.activate_session:
                self._session_state = SessionState.ENABLED
            if request.kind == PluginRequestKind.STEER_TURN:
                self._execution.request_steer()
            self.notify_inbox_changed()
            return CommandReceipt(CommandStatus.ACCEPTED, self.context_id)

    async def _prepare_run(self, run_id: str) -> bool:
        """输入交付前确认父会话仍允许开始工作。

        Args:
            run_id (str): Execution 已开始准备的轮次。
        Returns:
            bool: 明确停止或关闭后返回 False。
        """
        return self._session_state == SessionState.ENABLED

    async def _run_worker(self) -> None:
        """顺序处理本会话输入，结果先应用政策，再释放本次 Agent 资源。"""
        try:
            while self._session_state != SessionState.CLOSED:
                self._wakeup.clear()
                if self._session_state == SessionState.PAUSED:
                    await self._execution.finish_waiting()
                    await self._execution.close_current_agent()
                    await self._wakeup.wait()
                    continue
                request_id = self._resume_requests.pop(0) if self._resume_requests else None
                await self._execution.finish_requested_stop()
                waiting = self._execution.is_waiting()
                batch = () if request_id else await self._manager.read_activation_batch(
                    kind=InboxKind.HUMAN if waiting else None, include_queued=not waiting)
                if self._session_state != SessionState.ENABLED:
                    continue
                if not batch and request_id is None:
                    await self._wakeup.wait()
                    continue
                outcome = await self._execution.resume(request_id) if request_id else await self._execution.execute(batch, prepare=self._prepare_run)
                async with self._lock:
                    if self._session_state == SessionState.CLOSED:
                        break
                    if outcome.kind in {AgentRunOutcomeKind.FAILED, AgentRunOutcomeKind.SAVE_FAILED}:
                        self._failed_input_ids.update(item.id for item in batch if item.kind == InboxKind.HUMAN)
                        self._session_state = SessionState.PAUSED
                if outcome.kind != AgentRunOutcomeKind.INTERRUPTED:
                    await self._execution.close_current_agent()
        except Exception as exc:
            logger.exception("父会话工作循环失败：%s", self.context_id)
            await self._execution.handle_failure(exc)
            if self._session_state != SessionState.CLOSED:
                self._session_state = SessionState.PAUSED
            await self._execution.close_current_agent()
        finally:
            if self._session_state == SessionState.CLOSED:
                await self._execution.close("会话结束")

    def begin_close(self) -> None:
        """同步封闭受理入口并结束本执行，供应用在等待资源之前调用。"""
        self._session_state = SessionState.CLOSED
        self._execution.begin_close()
        self._wakeup.set()

    async def close(self, reason: str) -> None:
        """结束本父会话，按执行、Agent、Manager 的所有权顺序关闭。

        Args:
            reason (str): 父配置删除或应用退出原因。
        """
        self.begin_close()
        if self._worker is not None:
            await asyncio.gather(self._worker, return_exceptions=True)
        await self._execution.close(reason)
        await self._manager.close()
        self._events.put_nowait(SessionClosed(self.context_id, id(self)))
