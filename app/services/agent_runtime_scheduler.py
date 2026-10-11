"""管理各会话的后台处理任务，接收用户命令并把 Agent 输出交给界面。"""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from functools import partial
import logging
from typing import AsyncIterator, Callable
from uuid import uuid4

from app.agent.agent import Agent
from app.agent.core.event_router import AgentEvent, EventType
from app.agent.runtime.session_inbox import InboxEnvelope, SessionInbox
from app.agent.runtime.turn_control import TurnControl
from app.services.desktop_controller import DesktopController
from app.agent.state.manager import StateManager
from app.agent.state.model import AgentState
from app.agent.state.messages import HumanMessage, TaskResultMessage
from app.schemas.runtime_types import (
    ApprovalDecisionKind, AuthorizationResolutionKind, CommandKind, DESKTOP_CHANNEL,
    InboxDeliveryMode, InboxKind, InteractionEventKind, InterruptKind, SessionPhase,
    TaskResultStatus, TaskStatus, TurnInterruptReason, TurnPhase, RuntimePhase,
)
from app.services.interaction_contract import (
    ChannelCommand, CommandReceipt, InteractionEvent,
)
from app.services.interaction_event_hub import InteractionEventHub

from app.services.task_supervisor import TaskSupervisor


logger = logging.getLogger(__name__)
# worker 没有工作时最多等待 30 秒；这个间隔不是 Inbox 输入领取的有效期。
SESSION_IDLE_RECHECK_SECONDS = 30
# 多个后台任务结果合并给 Agent 时，限制每项和整条通知的正文长度。
TASK_NOTICE_ITEM_MAX_CHARS = 1024
TASK_NOTICE_BATCH_MAX_CHARS = 8192

# 将 Agent 的输出事件换成前端认识的事件名，例如 done 表示这轮完成，而不是关闭会话连接。
AGENT_TO_INTERACTION_EVENT = {
    EventType.TEXT_CHUNK: InteractionEventKind.TEXT_CHUNK,
    EventType.TOOL_CALL: InteractionEventKind.TOOL_CALL,
    EventType.INTERRUPT: InteractionEventKind.INTERRUPT,
    EventType.ERROR: InteractionEventKind.ERROR,
    EventType.DONE: InteractionEventKind.RUN_COMPLETED,
}


class AgentRuntimeScheduler:
    """协调会话执行、后台任务通知和应用启动关闭。

    每个会话最多有一个运行 _drain 的异步任务，称为 worker。worker 一次处理
    一轮回答，回答结束后继续等待；Agent 实例在每次执行或继续工具调用时创建。
    本类保存界面需要的运行状态；消息和待处理输入分别由 StateManager、Inbox 保存。

    Attributes:
        events (InteractionEventHub): 把回答片段、审批和排队变化发送给桌面及原输入渠道。
        _agent_factory (Callable[[str], Agent]): 根据会话创建实际调用模型和工具的 Agent。
        _inbox_factory (Callable[[str], SessionInbox]): 创建该会话的输入队列访问对象，读取同一数据库。
        _state_manager_factory (Callable[[str], StateManager]): 创建数据库访问对象，读取会话消息、审批和桌面结果。
        task_supervisor (TaskSupervisor | None): 启动、停止并记录进程任务，由所有会话共用。
        desktop (DesktopController | None): 应用共享的后端桌面模块，执行线程不属于单个会话。
        _tasks (dict[str, asyncio.Task[None]]): 每个会话运行 _drain 的后台异步任务，回答结束后仍保留等待。
        _wakeups (dict[str, asyncio.Event]): 新消息、审批决定、桌面回报或后台任务通知到达时，让对应 worker 结束等待、重新检查。
        _run_ids (dict[str, str]): 当前或下一轮回答的编号，用于确认停止、引导命令没有指向另一轮。
        _controls (dict[str, TurnControl]): 当前 Agent 的停止、引导信号和正在生成的文字，执行结束后移除。
        _phases (dict[str, SessionPhase]): 内存中各会话正在回答、等待审批、等待桌面结果、空闲或出错的状态。
        _origins (dict[str, str]): 各会话当前或最近一轮由哪个渠道发起，决定回答发给谁。
        _interrupts (dict[str, dict]): 内存中供界面显示的待审批或桌面请求，有效性仍以数据库为准。
        _resumes (dict[str, dict]): 当前轮次已保存的审批决定或桌面结果，等待 worker 交给 Agent 继续处理。
        _decision_locks (dict[str, asyncio.Lock]): 避免同一审批同时被批准、拒绝或新消息取消。
        _turn_locks (dict[str, asyncio.Lock]): 避免判断用户输入如何处理时，Agent 同时切换到完成或等待状态。
        _lock (asyncio.Lock): 避免并发通知为同一会话创建两个 worker。
        phase (RuntimePhase): 整个后端处于初始化、可执行、关闭中还是已关闭。
        _activated_sessions (set[str]): 本次启动后收到过新真人输入的会话，旧任务通知不能自行激活会话。
        _lifecycle_lock (asyncio.Lock): 避免启动清理或关闭期间，用户命令同时写入新的执行意图。
        _shutdown_partials (dict[str, tuple[str, bool, str, str | None]]): 关闭前记录的文字、内存消息记录标记、渠道和消息编号。
    """

    def __init__(
        self,
        event_hub: InteractionEventHub,
        *,
        agent_factory: Callable[[str], Agent],
        inbox_factory: Callable[[str], SessionInbox] = SessionInbox,
        state_manager_factory: Callable[[str], StateManager] = StateManager,
        task_supervisor: TaskSupervisor | None = None,
        desktop: DesktopController | None = None,
    ) -> None:
        """保存依赖和会话运行信息，保存共享桌面模块；此时不创建 worker。

        Args:
            event_hub (InteractionEventHub): 按来源路由的事件中心。
            agent_factory (Callable[[str], Agent]): 根据会话创建 Agent 的函数，只在实际执行时调用。
            inbox_factory (Callable[[str], SessionInbox]): 根据会话创建数据库输入队列访问对象的函数。
            state_manager_factory (Callable[[str], StateManager]): 根据会话创建 checkpoint 数据库访问对象的函数。
            task_supervisor (TaskSupervisor | None): 所有会话共用的进程任务管理器。
            desktop (DesktopController | None): 应用共享桌面模块，Scheduler 负责启动、停止与关闭。
        """
        self.events = event_hub
        self._agent_factory = agent_factory
        self._inbox_factory = inbox_factory
        self._state_manager_factory = state_manager_factory
        self.task_supervisor = task_supervisor
        self.desktop = desktop
        # 1. 按 session_id 保存各会话数据；聊天连接断开不删除它们。
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._wakeups: dict[str, asyncio.Event] = {}
        self._run_ids: dict[str, str] = {}
        self._controls: dict[str, TurnControl] = {}
        self._phases: dict[str, SessionPhase] = {}
        self._origins: dict[str, str] = {}
        self._interrupts: dict[str, dict] = {}
        self._resumes: dict[str, dict] = {}
        # 2. 用户操作和 Agent 输出都可能修改会话信息，分别保护审批决定和运行状态。
        self._decision_locks: dict[str, asyncio.Lock] = {}
        self._turn_locks: dict[str, asyncio.Lock] = {}
        self._lock = asyncio.Lock()
        self.phase = RuntimePhase.INITIALIZING
        self._activated_sessions: set[str] = set()
        self._lifecycle_lock = asyncio.Lock()
        self._shutdown_partials: dict[str, tuple[str, bool, str, str | None]] = {}
        self.events.set_snapshot_provider(self.snapshot)

    async def start(self) -> None:
        """清理上次退出的输入和轮次，保留任务事实，完成后才允许接收执行命令。

        Raises:
            RuntimeError: 已关闭的实例不能重新启动。
            Exception: 任一步清理失败时保留初始化阶段，禁止接收执行命令。
        """
        async with self._lifecycle_lock:
            if self.phase == RuntimePhase.RUNNING:
                return
            if self.phase != RuntimePhase.INITIALIZING:
                raise RuntimeError("已关闭的运行时不能重新启动")
            # 1. 删除上次尚未交给 Agent 的真人输入，保存旧工具的取消或未知结果，不继续旧任务。
            await self._reset_inbox()
            await self._finalize_abandoned_sessions()
            # 2. 上次仍显示 running 的进程已失去本次程序管理，将其记录为 orphaned 并保存通知。
            # 2.1. 此时还在初始化，即使任务结束回调到达，也不能创建 worker 或调用模型。
            if self.task_supervisor is not None:
                await self.task_supervisor.recover_orphaned()
            # 3. 数据库清理全部成功后才允许执行，前面任何异常都不会走到这里。
            if self.desktop is not None:
                self.desktop.start()
            self.phase = RuntimePhase.RUNNING

    async def _reset_inbox(self) -> None:
        """删除未交付的真人输入，并把已领取但未交付的任务通知恢复为待处理。"""
        inbox = self._inbox_factory("__runtime_init__")
        try:
            # 1. 这个辅助对象只提供数据库路径；清理针对全部会话，不创建真实用户输入。
            await SessionInbox.reset_after_restart(inbox.db_path)
        finally:
            await inbox.close()

    async def _finalize_abandoned_sessions(self) -> None:
        """找到数据库里未完成的轮次，只保存终止记录，不调用模型或执行工具。"""
        # 初始化用的会话只负责取得数据库路径并完成建表，不会开始回答或写入用户消息。
        manager = self._state_manager_factory("__runtime_init__")
        try:
            await manager.load()
            sessions = await StateManager.unfinished_sessions(manager.db_path)
        finally:
            await manager.close()
        # 1. 逐会话保存终止记录；保存失败就退出，下次只处理尚未完成的会话。
        # 1.1. 启动时没有旧进程文字；正常关闭时使用 _shutdown_partials 中刚捕获的文字。
        for session_id in sessions:
            manager = self._state_manager_factory(session_id)
            partial, persisted, origin, message_id = self._shutdown_partials.get(
                session_id, ("", False, DESKTOP_CHANNEL, None),
            )
            try:
                await Agent.finalize_abandoned_turn(
                    manager, partial=partial, partial_persisted=persisted,
                    origin_channel=origin, partial_message_id=message_id,
                )
            finally:
                await manager.close()

    async def notify_task_terminal(self, session_id: str) -> None:
        """任务结束通知已保存后，让本次收到过真人输入的会话检查 Inbox。

        Args:
            session_id (str): 后台任务所属会话；通知内容已由 Supervisor 保存到 Inbox。
        """
        # 1. 通知已经在数据库中；这里只让符合执行条件的 worker 停止等待、检查 Inbox。
        await self._ensure_running(session_id)

    async def begin_shutdown(self) -> None:
        """开始关闭：拒绝新操作，记下可见回答，取消 worker 并断开事件连接。"""
        async with self._lifecycle_lock:
            if self.phase in {RuntimePhase.STOPPING, RuntimePhase.STOPPED}:
                return
            # 1. 等已经开始受理的用户命令保存完成，再标记关闭；后来到达的命令会被拒绝。
            self.phase = RuntimePhase.STOPPING
            if self.desktop is not None:
                self.desktop.begin_shutdown()
            # 2. worker 退出会移除 TurnControl，先保存文字和消息编号，稍后才能写入聊天历史。
            for session_id, control in self._controls.items():
                self._shutdown_partials[session_id] = (
                    control.partial, control.partial_persisted,
                    self._origins.get(session_id, DESKTOP_CHANNEL), control.partial_message_id,
                )
            # 3. 请求 worker 取消，让模型、工具等待退出；已经发生的操作不能当作没有发生。
            # 3.1. close 会等待它们退出，再根据数据库中的可靠结果或未知结果记录收尾。
            for task in self._tasks.values():
                if not task.done():
                    task.cancel()
            # 4. 结束聊天事件连接，服务器关闭时不必继续等待这些长期 HTTP 请求。
            await self.events.close()

    async def snapshot(self, session_id: str, channel: str) -> dict:
        """返回运行状态、排队消息和审批，正文由界面读取聊天历史，不启动执行。

        Args:
            session_id (str): 共享 Ayaya 会话标识。
            channel (str): 已注册渠道标识。

        Returns:
            dict: 当前渠道可看到的运行状态、排队消息、审批和任务信息，不含回答正文。
        """
        # 1. 读取内存中的阶段、来源和待处理请求，不能顺便启动 Agent 或执行桌面动作。
        origin = self._origins.get(session_id)
        phase = self._phases.get(session_id, SessionPhase.IDLE)
        interrupt = self._interrupts.get(session_id)
        # 1.1. 在首次数据库等待前取得完整内存状态；空闲时的预分配编号只供命令回执使用。
        # EventHub 调用本方法时已持发布锁，状态事件会在快照发送后再提交。
        run_id = self._run_ids.get(session_id) if phase in {
            SessionPhase.RUNNING, SessionPhase.PENDING_INTERRUPT,
        } else None
        # 2. 桌面可以查看整个会话；其他渠道只查看自己发起的回答和自己提交的输入。
        visible = channel == DESKTOP_CHANNEL or channel == origin
        inbox = self._inbox_factory(session_id)
        try:
            pending = await inbox.pending_humans(
                origin_channel=None if channel == DESKTOP_CHANNEL else channel,
            )
        finally:
            await inbox.close()
        # 2.1. 队列内容来自数据库；input_sequence 让界面能够撤销指定排队消息。
        queued = [
            {"input_sequence": item.id, "message": item.payload["message"],
             "images": item.payload.get("images") or [],
             "origin_channel": item.origin_channel}
            for item in pending if item.delivery_mode == InboxDeliveryMode.QUEUED
        ]
        # 3. 回答属于其他渠道时，只给接收方自己的排队列表；这里的 idle 不代表整个会话空闲。
        if not visible:
            return {"phase": SessionPhase.IDLE, "queued_inputs": queued}
        return {
            "phase": phase,
            "run_id": run_id,
            "origin_channel": origin,
            "interrupt": interrupt,
            "pending_inputs": [
                {"input_sequence": item.id, "origin_channel": item.origin_channel,
                 "delivery_mode": item.delivery_mode}
                for item in pending
            ],
            "queued_inputs": queued,
            "tasks": [asdict(item) for item in await self.task_supervisor.list(session_id)]
            if channel == DESKTOP_CHANNEL and self.task_supervisor is not None else [],
        }

    async def submit(self, command: ChannelCommand) -> CommandReceipt:
        """后端可执行时受理命令；初始化和关闭期间拒绝新操作。

        Args:
            command (ChannelCommand): 当前渠道提交的用户命令。

        Returns:
            CommandReceipt: 持久受理结果或运行时不可执行的拒绝原因。
        """
        # 1. 与 start、begin_shutdown 使用同一把锁，检查状态后再受理，不能一边关闭一边写入新消息。
        async with self._lifecycle_lock:
            if self.phase != RuntimePhase.RUNNING:
                return CommandReceipt(False, "运行时尚未就绪或正在关闭")
            return await self._submit(command)

    async def _submit(self, command: ChannelCommand) -> CommandReceipt:
        """检查输入渠道，然后按命令种类调用对应方法。

        Args:
            command (ChannelCommand): 已由 submit 确认后端可执行的用户命令。

        Returns:
            CommandReceipt: 命令受理结果；回答内容由会话事件另外发送。
        """
        # 1. 每一种命令都检查来源，包括停止、撤销和审批，避免已关闭渠道继续操作会话。
        try:
            self.events.validate_source(command.origin_channel)
            if not await self.events.channel_enabled(command.session_id, command.origin_channel):
                raise ValueError("该会话未订阅此渠道")
        except ValueError as exc:
            return CommandReceipt(False, str(exc))

        # 2. 在这里展示命令与处理方法的对应关系，校验和执行细节放在各自方法中。
        handlers = {
            CommandKind.STOP_BACKGROUND_TASK: self._stop_background_task,
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
        return await handler(command)

    async def _stop_background_task(self, command: ChannelCommand) -> CommandReceipt:
        """停止同一会话的进程任务，不停止当前聊天回答。

        Args:
            command (ChannelCommand): 用户提交的命令，已通过渠道和后端运行状态检查。

        Returns:
            CommandReceipt: 是否已受理，以及不能受理的原因。
        """
        # 1. 先确认任务管理器和任务编号存在，不把缺少编号当作停止全部任务。
        if self.task_supervisor is None or not command.task_id:
            return CommandReceipt(False, "后台任务不存在")
        # 2. 查数据库确认任务属于当前会话，不能从一个会话停止另一个会话的进程。
        record = await self.task_supervisor.get(command.task_id)
        if record is None or record.session_id != command.session_id:
            return CommandReceipt(False, "后台任务不存在")
        # 3. Supervisor 请求进程退出，实际结束和结果通知稍后由任务管理器记录。
        return CommandReceipt(await self.task_supervisor.stop(command.task_id))

    async def _cancel_queued_input(self, command: ChannelCommand) -> CommandReceipt:
        """撤销当前渠道尚未开始处理的排队消息，并通知界面移除它。

        Args:
            command (ChannelCommand): 用户提交的命令，已通过渠道和后端运行状态检查。

        Returns:
            CommandReceipt: 是否已受理，以及不能受理的原因。
        """
        # 1. 必须指定 Inbox 输入编号，不能只按消息正文删除同名排队消息。
        sequence = command.payload.get("input_sequence")
        if not isinstance(sequence, int):
            return CommandReceipt(False, "缺少排队消息编号")
        inbox = self._inbox_factory(command.session_id)
        try:
            # 2. 数据库只删除同来源、排队且尚未领取的输入；已开始处理的消息不能撤销。
            cancelled = await inbox.cancel_queued(sequence, command.origin_channel)
        finally:
            await inbox.close()
        if cancelled:
            # 3. 删除成功后才通知界面移除这一项，失败时保留原显示并返回原因。
            await self.events.publish(InteractionEvent(
                command.session_id, InteractionEventKind.QUEUED_CANCELLED,
                {"input_sequence": sequence},
                origin_channel=command.origin_channel,
            ))
        return CommandReceipt(cancelled, None if cancelled else "排队消息已开始或不存在")

    async def _stop_current_turn(self, command: ChannelCommand) -> CommandReceipt:
        """请求当前回答停止；保留会话 worker，以便继续处理后续输入。

        Args:
            command (ChannelCommand): 用户提交的命令，已通过渠道和后端运行状态检查。

        Returns:
            CommandReceipt: 是否已受理，以及不能受理的原因。
        """
        # 1. 会话必须正在回答，指定 run_id 时还必须匹配，旧停止按钮不能影响新回答。
        control = self._controls.get(command.session_id)
        if (control is None or self._phases.get(command.session_id) != SessionPhase.RUNNING
                or (command.target_run_id and command.target_run_id != control.run_id)):
            return CommandReceipt(False, "没有匹配的活动轮次")
        # 2. 这里只记录停止请求，不取消 worker；Agent 在可安全保存结果的位置结束当前回答。
        # 2.1. 最终保存已经开始时拒绝停止，避免界面报告已停止而数据库已经保存正常完成。
        if not control.request(TurnInterruptReason.STOP):
            return CommandReceipt(False, "当前轮次正在提交，已无法停止")
        return CommandReceipt(True, run_id=control.run_id)

    async def _submit_human_input(self, command: ChannelCommand) -> CommandReceipt:
        """保存新消息、引导或排队输入，再通知会话 worker 处理。

        Args:
            command (ChannelCommand): 用户提交的命令，已通过渠道和后端运行状态检查。

        Returns:
            CommandReceipt: 是否已受理，以及不能受理的原因。
        """
        # 1. 引导必须说明针对哪轮回答；文字和图片都为空时不保存输入。
        if command.kind == CommandKind.HUMAN_STEER and not command.target_run_id:
            return CommandReceipt(False, "引导缺少目标轮次")
        message = command.payload.get("message")
        images = command.payload.get("images")
        if not isinstance(message, str) or (not message.strip() and not images):
            return CommandReceipt(False, "消息不能为空")
        # 2. 与 Agent 更新运行状态使用同一把锁，避免判断可排队时这轮已经结束。
        turn_lock = self._turn_locks.setdefault(command.session_id, asyncio.Lock())
        async with turn_lock:
            phase = self._phases.get(command.session_id)
            control = self._controls.get(command.session_id)
            committing = control is not None and control.phase == TurnPhase.COMMITTING
            # 2.1. 只有已有回答正在运行或等待审批、桌面结果时才有排队的对象。
            if command.kind == CommandKind.QUEUE_TURN and phase not in {
                SessionPhase.RUNNING, SessionPhase.PENDING_INTERRUPT,
            }:
                return CommandReceipt(False, "当前没有可排队的活动轮次")
            # 2.1.1. 已开始最终保存时不再接受排队，用户可以把这条消息直接作为下一轮输入。
            if command.kind == CommandKind.QUEUE_TURN and committing:
                return CommandReceipt(False, "当前轮次正在提交，请作为新消息发送")
            # 2.2. 明确要求排队就排队；有效引导稍后请求当前回答停止，等待新指令接替。
            queued = command.kind == CommandKind.QUEUE_TURN
            if command.kind == CommandKind.HUMAN_STEER:
                target_is_active = (
                    phase == SessionPhase.RUNNING
                    and command.target_run_id == self._run_ids.get(command.session_id)
                    and not committing
                )
                # 2.3. 引导指向的回答已过期时不能打断另一个回答，有活动轮次则转成排队。
                # 2.4. 空闲、出错或正在最终保存时，保留为下一次普通输入，而不是继续旧轮次。
                if not target_is_active and not committing and phase in {
                    SessionPhase.RUNNING, SessionPhase.PENDING_INTERRUPT,
                }:
                    queued = True
            # 3. 先把输入保存到 Inbox，才请求停止当前回答；保存失败时原回答继续运行。
            inbox = self._inbox_factory(command.session_id)
            try:
                envelope = await inbox.submit_human(
                    command.origin_channel, message, images=images,
                    queued=queued,
                )
            finally:
                await inbox.close()
            # 3.1. 保存成功才获得本次启动的会话执行资格，旧任务通知不能自己获得此资格。
            self._activated_sessions.add(command.session_id)
            # 3.2. 新消息或有效引导接替当前回答；已开始的工具先返回并保存结果，再结束旧回答。
            if not queued and phase == SessionPhase.RUNNING:
                if control is not None and (
                    command.kind != CommandKind.HUMAN_STEER or control.run_id == command.target_run_id
                ):
                    control.request(TurnInterruptReason.STEER)
        # 4. 排队输入先显示在排队列表中，不假装已经进入聊天历史。
        # 4.1. 普通真人消息由 Agent 保存成功后，通过 _publish_committed_human_message 通知界面。
        if queued:
            await self.events.publish(InteractionEvent(
                command.session_id, InteractionEventKind.QUEUED_MESSAGE,
                {"input_sequence": envelope.id, "message": message,
                 "images": images or []},
                origin_channel=command.origin_channel,
            ))
        # 5. 等待审批时收到直接发送的新指令，取消旧审批；排队消息则继续等待审批处理。
        if not queued and phase == SessionPhase.PENDING_INTERRUPT:
            await self._cancel_pending_interrupt(command.session_id, command.origin_channel)
        # 6. 输入已在数据库中，通知 worker 处理；回执不表示模型已收到或回答已完成。
        await self._ensure_running(command.session_id)
        return CommandReceipt(True, run_id=self._run_ids.get(command.session_id))

    async def _cancel_pending_interrupt(self, session_id: str, actor: str) -> None:
        """等待审批时收到新消息，取消旧审批并通知界面结束旧轮次。

        AgentState 记录未决中断的首次取消，本方法保留决定锁、状态保存和事件顺序。
        正在生成的回答与执行中的工具仍由 TurnControl 处理。

        Args:
            session_id (str): 共享 Ayaya 会话标识。
            actor (str): 发送新消息的渠道，用于告诉界面是谁取消了旧审批。
        """
        # 1. 与批准、拒绝使用同一把锁；如果用户已经决定，就不能再用新消息覆盖决定。
        lock = self._decision_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            manager = self._state_manager_factory(session_id)
            try:
                state = await manager.load()
                interrupt = state.interrupt_data or {}
                # 2. 由状态对象判断是否仍可取消；先保存，迟到决定才能按持久状态被拒绝。
                if not state.cancel_pending_interrupt():
                    return
                await manager.save(state, checkpoint_type="intermediate")
                await self._publish_runtime_event(InteractionEvent(
                    session_id, InteractionEventKind.AUTHORIZATION_RESOLVED,
                    {"outcome": AuthorizationResolutionKind.CANCELLED, "actor_channel": actor},
                    origin_channel=str(interrupt.get("origin_channel") or DESKTOP_CHANNEL),
                    request_id=str(interrupt.get("request_id") or ""),
                ))
                # 3. 结束旧回答的显示；新消息还在 Inbox，worker 取出后由 Agent 补齐旧工具取消结果。
                previous_run_id = self._run_ids.get(session_id)
                await self._publish_runtime_event(InteractionEvent(
                    session_id, InteractionEventKind.RUN_COMPLETED, {"value": {"interrupted": True}},
                    origin_channel=str(interrupt.get("origin_channel") or DESKTOP_CHANNEL),
                    run_id=previous_run_id,
                ))
            finally:
                await manager.close()

    async def _resolve_authorization(self, command: ChannelCommand) -> CommandReceipt:
        """处理当前审批的批准、拒绝或改发新指令，重复决定和旧请求一律拒绝。

        Scheduler 校验命令并协调锁、Inbox 和 checkpoint 保存；AgentState 负责
        当前审批的编号与首次决定规则，保存成功后才发布事件和安排恢复。

        Args:
            command (ChannelCommand): 统一渠道命令或需要执行的命令正文，类型决定其含义。

        Returns:
            CommandReceipt: 命令受理结果，后续回复通过事件订阅取得。
        """
        # 1. 只接受批准、拒绝或引导；引导必须包含新的指令正文。
        decision = command.payload
        if decision.get("kind") not in {ApprovalDecisionKind.APPROVE, ApprovalDecisionKind.REJECT, ApprovalDecisionKind.STEER}:
            return CommandReceipt(False, "无效的审批决定")
        if decision.get("kind") == ApprovalDecisionKind.STEER and not str(decision.get("message") or "").strip():
            return CommandReceipt(False, "引导消息不能为空")
        steer_envelope = None
        lock = self._decision_locks.setdefault(command.session_id, asyncio.Lock())
        # 2. 加锁后重新读取数据库，确认审批编号仍匹配，而且还没有人处理过它。
        async with lock:
            manager = self._state_manager_factory(command.session_id)
            try:
                state = await manager.load()
                interrupt = state.get_pending_authorization(command.request_id)
                if interrupt is None:
                    return CommandReceipt(False, "审批请求已处理或过期")
                origin = str(interrupt.get("origin_channel") or DESKTOP_CHANNEL)
                # 2.1. 用户改发新指令时先保存新指令，不能只关闭旧审批却丢掉新消息。
                if decision["kind"] == ApprovalDecisionKind.STEER:
                    inbox = self._inbox_factory(command.session_id)
                    try:
                        steer_envelope = await inbox.submit_human(
                            command.origin_channel, decision["message"],
                        )
                    finally:
                        await inbox.close()
                # 3. 状态对象记录首次决定，保存成功后才通知原回答所属渠道关闭审批。
                if not state.record_authorization_decision(command.request_id, decision):
                    return CommandReceipt(False, "审批请求已处理或过期")
                await manager.save(state, checkpoint_type="intermediate")
                await self._publish_runtime_event(InteractionEvent(
                    command.session_id, InteractionEventKind.AUTHORIZATION_RESOLVED,
                    {"outcome": AuthorizationResolutionKind(decision["kind"]), "actor_channel": command.origin_channel},
                    origin_channel=origin,
                    request_id=command.request_id,
                ))
                # 4. 批准、拒绝都交给 worker 继续处理工具结果；引导则结束旧回答，用新指令另起一轮。
                if decision["kind"] != ApprovalDecisionKind.STEER:
                    self._resumes[command.session_id] = {"decision": decision, "origin": origin}
                else:
                    previous_run_id = self._run_ids.get(command.session_id)
                    await self._publish_runtime_event(InteractionEvent(
                        command.session_id, InteractionEventKind.RUN_COMPLETED,
                        {"value": {"interrupted": True}},
                        origin_channel=origin, run_id=previous_run_id,
                    ))
            finally:
                await manager.close()
        if decision["kind"] == ApprovalDecisionKind.STEER:
            assert steer_envelope is not None
            self._activated_sessions.add(command.session_id)
            await self._ensure_running(command.session_id)
            return CommandReceipt(True, run_id=self._run_ids.get(command.session_id))
        await self._ensure_running(command.session_id)
        return CommandReceipt(True, run_id=self._run_ids.get(command.session_id))


    async def _ensure_running(self, session_id: str) -> None:
        """为有新真人输入的会话创建 worker；已经存在时只通知它检查新工作。

        Args:
            session_id (str): 共享 Ayaya 会话标识。
        """
        # 1. 新消息和任务结束可能同时到达，加锁确保同一个会话只创建一个 _drain 任务。
        async with self._lock:
            # 1.1. 本次没有新真人输入的会话，或初始化、关闭中的后端，不允许启动 Agent。
            if self.phase != RuntimePhase.RUNNING or session_id not in self._activated_sessions:
                return
            task = self._tasks.get(session_id)
            # 1.2. 正在等待或执行的 worker 都直接复用，只有不存在或已退出时才重新创建。
            if task is None or task.done():
                self._run_ids[session_id] = uuid4().hex
                self._wakeups[session_id] = asyncio.Event()
                self._tasks[session_id] = asyncio.create_task(self._drain(session_id))
            elif self._phases.get(session_id) in {SessionPhase.IDLE, SessionPhase.ERROR} and session_id not in self._run_ids:
                self._run_ids[session_id] = uuid4().hex
            # 2. set 通知 _next_input_batch 结束等待；消息内容仍在 Inbox，审批和桌面结果在 _resumes。
            self._wakeups[session_id].set()

    def _update_snapshot_state(self, event: InteractionEvent) -> None:
        """在 EventHub 发布锁内同步更新运行状态，不维护重连正文缓存。

        由 _publish_runtime_event 提供给 EventHub 调用；调用前已取得会话运行锁
        和发布锁。本方法只修改内存，不等待、不保存 checkpoint、不取得其他锁。

        Args:
            event (InteractionEvent): 即将取得发布编号并发送给界面的事件。
        """
        session_id = event.session_id
        # 1. 开始事件一起建立运行编号、来源和阶段，快照不能看到半份开始状态。
        if event.kind == InteractionEventKind.RUN_STARTED:
            self._run_ids[session_id] = event.run_id
            self._origins[session_id] = event.origin_channel or DESKTOP_CHANNEL
            self._phases[session_id] = SessionPhase.RUNNING
        # 2. 等待请求和审批解除同步修改展示状态，持久保存仍由调用方先完成。
        elif event.kind == InteractionEventKind.INTERRUPT:
            interrupt = event.payload["value"]
            self._phases[session_id] = SessionPhase.PENDING_INTERRUPT
            self._interrupts[session_id] = interrupt
        elif event.kind == InteractionEventKind.AUTHORIZATION_RESOLVED:
            self._interrupts.pop(session_id, None)
        # 3. 终态与当前编号一起变更，不能先发完成事件再留下可被快照读到的旧编号。
        elif event.kind == InteractionEventKind.RUN_COMPLETED:
            self._phases[session_id] = SessionPhase.IDLE
            self._interrupts.pop(session_id, None)
            self._run_ids.pop(session_id, None)
        elif event.kind == InteractionEventKind.ERROR:
            self._phases[session_id] = SessionPhase.ERROR
            self._run_ids.pop(session_id, None)

    async def _publish_runtime_event(self, event: InteractionEvent) -> None:
        """按运行锁、发布锁的顺序提交界面状态和事件，保持快照与增量一致。

        Args:
            event (InteractionEvent): 已经准备好的运行事件，数据库前置写入已由调用方完成。
        """
        # 1. 输入提交也读取运行状态，继续用原运行锁保护；快照不能反向取得这把锁。
        async with self._turn_locks.setdefault(event.session_id, asyncio.Lock()):
            # 2. EventHub 取得发布锁后才调用同步回调，随后分配编号并入队，中间没有等待。
            await self.events.publish(
                event, update_snapshot_state=partial(self._update_snapshot_state, event),
            )

    async def _consume_events(
        self, session_id: str, origin: str, run_id: str,
        stream: AsyncIterator[AgentEvent],
    ) -> None:
        """逐条读取 Agent 输出，同步运行状态，再交给 EventHub 发送给界面。

        Args:
            session_id (str): 共享 Ayaya 会话标识。
            origin (str): 本轮可见事件的原渠道。
            run_id (str): 当前运行轮次标识。
            stream (AsyncIterator[AgentEvent]): Agent.run、resume 或 run_runtime_inputs 产生的事件流。
        """
        try:
            # 1. worker 持续读取 Agent 输出，聊天页面断线不会停止这个循环。
            async for event in stream:
                # 2. 等待状态和终态与事件编号一起更新，文字仅转发，不复制到快照。
                await self._publish_runtime_event(InteractionEvent(
                    session_id,
                    AGENT_TO_INTERACTION_EVENT[event.type],
                    {"value": event.data}, origin_channel=origin, run_id=run_id,
                    request_id=(event.data or {}).get("request_id")
                    if event.type == EventType.INTERRUPT else None,
                ))
        # 4. 执行出错时记录错误并告知界面，已经写入数据库的消息和工具结果不删除。
        except Exception as exc:
            logger.exception("会话 %s 的运行失败", session_id)
            await self._publish_runtime_event(InteractionEvent(
                session_id, InteractionEventKind.ERROR, {"detail": str(exc)},
                origin_channel=origin, run_id=run_id,
            ))

    @staticmethod
    def _task_result_message(claims: tuple[InboxEnvelope, ...]) -> TaskResultMessage:
        """把 Inbox 中的任务结束通知整理成 Agent 能读取的 TaskResultMessage。

        Args:
            claims (tuple[InboxEnvelope, ...]): 本次从 Inbox 领取的任务通知，包含任务编号、状态和输出摘要。

        Returns:
            TaskResultMessage: 任务完成、失败、停止或未知的结果消息，不伪装成用户发送的消息。
        """
        # 1. 一条通知直接转成消息，stopped 表示取消，orphaned 表示结果未知，不能当作成功。
        if len(claims) == 1:
            payload = claims[0].payload
            status = payload.get("status")
            return TaskResultMessage(
                task_id=str(payload["task_id"]), task_name=str(payload["task_name"]),
                status=(TaskResultStatus.CANCELLED if status == TaskStatus.STOPPED else
                        TaskResultStatus.COMPLETED if status == TaskStatus.COMPLETED else
                        TaskResultStatus.ORPHANED if status == TaskStatus.ORPHANED else TaskResultStatus.FAILED),
                result=str(payload.get("result") or ""),
            )
        # 2. 多条通知合并成一条消息；状态相同则保留该状态，不同时用 mixed，并限制摘要长度。
        statuses = {item.payload.get("status") for item in claims}
        mapped_statuses = {
            TaskResultStatus.CANCELLED if status == TaskStatus.STOPPED else
            TaskResultStatus.COMPLETED if status == TaskStatus.COMPLETED else
            TaskResultStatus.ORPHANED if status == TaskStatus.ORPHANED else TaskResultStatus.FAILED
            for status in statuses
        }
        combined_status = mapped_statuses.pop() if len(mapped_statuses) == 1 else TaskResultStatus.MIXED
        details = "\n".join(
            f"{item.payload['task_id']} [{item.payload.get('status')}]: "
            f"{str(item.payload.get('result') or '')[:TASK_NOTICE_ITEM_MAX_CHARS]}"
            for item in claims
        )
        return TaskResultMessage(
            task_id="batch:" + ",".join(str(item.payload["task_id"]) for item in claims),
            task_name=f"{len(claims)} 个后台任务",
            status=combined_status,
            result=details[:TASK_NOTICE_BATCH_MAX_CHARS],
        )

    async def _drain(self, session_id: str) -> None:
        """持续处理一个会话：继续等待中的工具，或取出新输入交给 Agent。

        每个会话只有一个运行本方法的异步任务，即 worker。一次回答结束后继续
        等待下一份工作；只有应用关闭或任务退出时，才关闭这个 Inbox 访问对象。

        Args:
            session_id (str): 当前 worker 负责的会话。
        """
        inbox = self._inbox_factory(session_id)
        try:
            while True:
                # 1. 用户已作出审批决定时先继续原工具调用，桌面输入在后端直接返回结果。
                # 1.1. pop 取走待处理数据，防止下一次循环再次处理同一决定或结果。
                resume = self._resumes.pop(session_id, None)
                if resume is not None:
                    await self._resume_interrupted_turn(session_id, inbox, resume)
                    continue

                # 2. 没有待继续的工具时，取一条真人输入和当前可交付的任务通知。
                # 2.1. 若当前没有输入，这个方法等待新消息、审批决定、桌面结果或任务通知。
                batch = await self._next_input_batch(session_id, inbox)
                if not batch:
                    # 2.2. 等待结束后从步骤 1 重新检查，审批或桌面结果可能已在等待期间到达。
                    continue

                # 3. 将取出的输入交给一个 Agent；处理结束后关闭 Agent，再等待下一轮工作。
                await self._run_input_batch(session_id, inbox, batch)
        finally:
            # 4. 正常退出、取消或异常都会关闭连接；这里不删除消息，也不标记输入已交付。
            # 4.1. 应用退出时，close 另行作废未交付真人输入，并保留任务通知。
            await inbox.close()

    async def _claim_ready_inputs(
        self, inbox: SessionInbox, phase: SessionPhase | None,
    ) -> tuple[InboxEnvelope, ...]:
        """按当前会话等待状态，领取这一次允许交给 Agent 的输入。

        Args:
            inbox (SessionInbox): 当前 worker 使用的会话输入队列。
            phase (SessionPhase | None): 本次检查开始时的会话状态，两次检查采用相同规则。

        Returns:
            tuple[InboxEnvelope, ...]: 当前领取的输入；没有可处理输入时为空。
        """
        # 1. 等待审批时，只有直接发送的新真人消息可以取代审批；排队消息和任务通知继续等。
        return await inbox.claim_activation_batch(
            kind=InboxKind.HUMAN if phase == SessionPhase.PENDING_INTERRUPT else None,
            include_queued=phase != SessionPhase.PENDING_INTERRUPT,
        )

    async def _next_input_batch(
        self, session_id: str, inbox: SessionInbox,
    ) -> tuple[InboxEnvelope, ...]:
        """取出可处理输入；没有输入时等待通知，避免 worker 持续空转。

        Args:
            session_id (str): 当前 worker 负责的会话。
            inbox (SessionInbox): 当前会话的持久输入队列。

        Returns:
            tuple[InboxEnvelope, ...]: 领取到的输入；等待结束后返回空值，由主循环重新检查工具结果。
        """
        # 1. 先读数据库，消息可能在本次 worker 开始等待之前就已经保存。
        phase = self._phases.get(session_id)
        batch = await self._claim_ready_inputs(inbox, phase)
        if batch:
            return batch

        # 2. 清除上次通知后再读一次数据库，不能直接清除通知后就进入等待。
        # 2.1. 若新消息恰好在第一次查询和 clear 之间到达，通知会被清掉，但第二次查询能取到消息。
        wakeup = self._wakeups[session_id]
        wakeup.clear()
        batch = await self._claim_ready_inputs(inbox, phase)
        if batch:
            return batch

        # 3. 第二次查询后到达的消息会 set 这个 Event，让等待立即结束；最长每 30 秒复查一次。
        # 3.1. 等待结束不直接执行任务；主循环先检查是否有审批决定或桌面结果需要处理。
        try:
            await asyncio.wait_for(wakeup.wait(), SESSION_IDLE_RECHECK_SECONDS)
        except asyncio.TimeoutError:
            pass
        return ()

    async def _resume_interrupted_turn(
        self, session_id: str, inbox: SessionInbox, resume: dict,
    ) -> None:
        """把已保存的审批决定或桌面结果交给 Agent，继续本次进程中的原轮次。

        Args:
            session_id (str): 等待工具继续处理的会话。
            inbox (SessionInbox): 用于读取本轮期间到达的任务通知。
            resume (dict): 原来源渠道，以及已保存的审批决定或桌面执行结果。
        """
        # 1. 恢复原轮次来源并显示正在运行；等待期间关闭的 Agent 实例需要重新创建。
        origin = resume["origin"]
        # 1.1. 审批或桌面等待期间保留的编号继续使用，不能把这次处理误关联到另一轮。
        run_id = self._run_ids.get(session_id) or uuid4().hex
        await self._publish_runtime_event(InteractionEvent(
            session_id, InteractionEventKind.RUN_STARTED, {"resumed": True},
            origin_channel=origin, run_id=run_id,
        ))
        agent = self._agent_factory(session_id)
        control = TurnControl(run_id)
        self._controls[session_id] = control

        # 2. Agent 每次准备调用模型时，用这个回调加入刚结束的后台任务结果。
        # 2.1. partial 先记住当前 Agent 和 Inbox；Agent 稍后调用它时，再传入当时的 state。
        before_next_llm = partial(self._deliver_task_notices_before_model, agent, inbox)

        try:
            # 3. 把已经保存的审批决定或桌面回报交给 Agent，逐条处理它继续生成的输出。
            await self._consume_events(
                session_id, origin, run_id,
                agent.resume(
                    resume["decision"], turn_control=control,
                    before_next_llm=before_next_llm,
                ),
            )
            # 4. 原轮次正常完成才放行排队消息；用户停止、改指令或执行出错时继续等待。
            if self._phases.get(session_id) == SessionPhase.IDLE and control.reason is None:
                await inbox.release_queued()
        finally:
            # 5. 关闭这次 Agent，移除它的停止控制；外层 worker 继续处理本会话下一份工作。
            self._controls.pop(session_id, None)
            await agent.close()

    async def _run_input_batch(
        self, session_id: str, inbox: SessionInbox, batch: tuple[InboxEnvelope, ...],
    ) -> None:
        """执行本次领取的一条真人消息及任务通知，或只执行任务通知。

        Args:
            session_id (str): 当前 worker 负责的会话。
            inbox (SessionInbox): 用于本轮任务通知读取和排队消息放行。
            batch (tuple[InboxEnvelope, ...]): 至多一条真人输入，以及当前可交付的任务通知。
        """
        # 1. 区分真人消息和任务结果，建立这轮的编号、来源和界面状态，再创建 Agent。
        human = next((item for item in batch if item.kind == InboxKind.HUMAN), None)
        terminals = tuple(item for item in batch if item.kind == InboxKind.TASK_TERMINAL)
        # 1.1. 有真人消息时按它的渠道回复；只有任务通知时，结果显示在桌面。
        run_id = self._run_ids.get(session_id) or uuid4().hex
        origin = human.origin_channel if human is not None else DESKTOP_CHANNEL
        await self._publish_runtime_event(InteractionEvent(
            session_id, InteractionEventKind.RUN_STARTED, {},
            origin_channel=origin, run_id=run_id,
        ))
        agent = self._agent_factory(session_id)
        control = TurnControl(run_id)
        self._controls[session_id] = control

        # 2. 本次执行期间新到达的任务通知，等下一次模型调用前再加入。
        # 2.1. 绑定当前 Agent 和 Inbox；调用时的 state 由 Agent 提供，不能用执行前的旧状态代替。
        before_next_llm = partial(self._deliver_task_notices_before_model, agent, inbox)

        try:
            # 3. 根据这次领取的输入选择 Agent 入口，一条真人消息只启动一次回答。
            if human is not None:
                # 3.1. 这里只绑定回调；Agent 保存真人消息成功后才调用，不能提前显示为已交付。
                on_input_committed = partial(self._publish_committed_human_message, session_id, human)

                # 3.2. 有任务通知时，把通知和真人消息一起保存；Agent 会先加入任务事实，再加入真人正文。
                runtime_kwargs = (
                    {"inbox_claims": batch,
                     "runtime_inputs": (self._task_result_message(terminals),)}
                    if terminals else {}
                )
                # 3.3. 此处取得异步事件流；下一步 _consume_events 读取时才真正执行 Agent。
                stream = agent.run(
                    human.payload["message"],
                    images=human.payload.get("images"),
                    origin_channel=origin,
                    inbox_claim=human,
                    turn_control=control,
                    before_next_llm=before_next_llm,
                    on_input_committed=on_input_committed,
                    **runtime_kwargs,
                )
            else:
                # 3.4. 只有任务通知就执行内部轮次，不增加真人消息计数；会话已在本次被真人激活。
                stream = agent.run_runtime_inputs(
                    (self._task_result_message(terminals),),
                    inbox_claims=terminals,
                    turn_control=control,
                    before_next_llm=before_next_llm,
                )
            # 4. 持续读取并发布回答事件；正常完成真人轮次后才放行排队消息。
            # 4.1. 只有任务通知、用户停止、引导或错误结束时，不放行此前等待的排队消息。
            await self._consume_events(session_id, origin, run_id, stream)
            if (human is not None and self._phases.get(session_id) == SessionPhase.IDLE
                    and control.reason is None):
                await inbox.release_queued()
        # 5. Agent 执行或输入保存失败时告诉界面出错，未成功交付的输入仍由 Inbox 保留。
        except Exception as exc:
            logger.exception("会话 %s 的运行失败", session_id)
            await self._publish_runtime_event(InteractionEvent(
                session_id, InteractionEventKind.ERROR, {"detail": str(exc)},
                origin_channel=origin, run_id=run_id,
            ))
        finally:
            # 6. 关闭本次 Agent，移除旧的停止控制；worker 随后继续等待和处理新工作。
            self._controls.pop(session_id, None)
            await agent.close()

    async def _deliver_task_notices_before_model(
        self, agent: Agent, inbox: SessionInbox, state: AgentState,
    ) -> None:
        """在下一次模型调用前，把刚结束的后台任务结果加入当前 Agent 的消息。

        Agent 通过 before_next_llm 调用本方法；正在执行的工具不会被此回调打断。

        Args:
            agent (Agent): 正在处理当前轮次的 Agent。
            inbox (SessionInbox): 保存后台任务结束通知的会话输入队列。
            state (AgentState): 工具结果已处理完、准备发送给模型的当前消息状态。
        """
        # 1. 领取此刻已有的任务通知，不领取真人消息，也不等待尚未结束的任务。
        claims = await inbox.claim_task_terminals()
        if not claims:
            return
        # 2. 任务结果加入当前消息，并与 Inbox 的 delivered 标记一起保存，避免重复交付。
        await agent.commit_runtime_inputs(state, (self._task_result_message(claims),), claims)

    async def _publish_committed_human_message(
        self, session_id: str, input_record: InboxEnvelope, message: HumanMessage,
    ) -> None:
        """真人消息保存成功后通知界面显示，不必等待模型生成第一段回答。

        Agent 通过 on_input_committed 调用本方法；此时 checkpoint 和 Inbox delivered 已保存。

        Args:
            session_id (str): 消息所属会话。
            input_record (InboxEnvelope): 原 Inbox 输入，提供编号和上传的图片。
            message (HumanMessage): 已保存的真人消息，提供正文、来源和稳定消息编号。
        """
        # 1. 使用已保存消息的编号，让前端能与聊天历史去重；不能另造一条真人消息。
        await self.events.publish(InteractionEvent(
            session_id, InteractionEventKind.HUMAN_MESSAGE,
            {"message": message.text, "images": input_record.payload.get("images") or [],
             "input_sequence": input_record.id, "message_id": message.message_id},
            origin_channel=message.origin_channel,
        ))

    async def close(self) -> None:
        """等待 worker 退出，保存回答终止记录和任务通知，再关闭运行时。"""
        await self.begin_shutdown()
        async with self._lifecycle_lock:
            if self.phase == RuntimePhase.STOPPED:
                return
            # 1. 等 worker 和桌面线程的清理都结束，才能读取最后状态、保存终止记录。
            if self._tasks:
                await asyncio.gather(*self._tasks.values(), return_exceptions=True)
            if self.desktop is not None:
                await self.desktop.close()
            # 2. 停止后台进程并保存结果；已经处于 STOPPING，通知到达也不会再创建 Agent。
            # 2.1. 保存刚捕获的回答文字和旧工具终止结果，删除剩余真人输入，任务通知继续留存。
            if self.task_supervisor is not None:
                await self.task_supervisor.close()
            await self._finalize_abandoned_sessions()
            await self._reset_inbox()
            # 3. 数据库收尾成功后清空内存中的待处理决定和回答状态，最后标记 STOPPED。
            self._resumes.clear()
            self._activated_sessions.clear()
            self._run_ids.clear()
            self._interrupts.clear()
            self._phases.clear()
            self._shutdown_partials.clear()
            self.phase = RuntimePhase.STOPPED
