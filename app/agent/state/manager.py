"""协调会话输入、执行状态和遗留轮次的业务提交，不持有执行或调度对象。"""

from __future__ import annotations

import asyncio
from dataclasses import replace
import json
import logging

from app.agent.runtime.session_inbox import InboxEnvelope
from app.agent.state.model import AgentState
from app.agent.state.messages import (
    AssistantMessage, HumanMessage, ImageContent, RuntimeInputMessage, ToolMessage,
    TaskResultMessage, text_content, with_text,
)
from app.agent.utils.domain.images import save_multiple_images
from app.crud.agent_state_dao import AgentStateDao, CheckpointType
from app.crud.session_inbox_dao import SessionInboxDao
from app.schemas.runtime_types import DESKTOP_CHANNEL, InboxKind, InboxStatus, InterruptKind, TaskResultStatus, TaskStatus

logger = logging.getLogger(__name__)

INTERRUPTED_TOOL_RESULT = "上次工具链中断，未获得该工具的可靠结果；为避免重复副作用，本次未自动重试。"
USER_REJECTED_INTERRUPTED_TOOL_RESULT = "用户发送了新的消息，待批准工具调用已拒绝。"
USER_CANCELLED_PENDING_TOOL_RESULT = "用户发送了新的消息，本批后续工具调用已取消。"
APPLICATION_EXIT_RESULT = "应用退出，本轮已终止；本次工具调用已取消。"
APPLICATION_EXIT_UNKNOWN_RESULT = "应用退出，本轮已终止；工具执行结果未知，本次不会自动重试。"

TASK_NOTICE_BATCH_MAX_CHARS = 8192


def _task_result_message(inputs: tuple[InboxEnvelope, ...]) -> TaskResultMessage:
    """把 Inbox 中的任务结束通知整理成 Agent 能读取的 TaskResultMessage。

    Args:
        inputs (tuple[InboxEnvelope, ...]): 本次从 Inbox 读取的任务通知，包含任务编号、状态和日志引用。

    Returns:
        TaskResultMessage: 任务完成、失败、停止或未知的结果消息，不伪装成用户发送的消息。
    """
    # 1. 一条通知直接转成消息，stopped 表示取消，orphaned 表示结果未知，不能当作成功。
    if len(inputs) == 1:
        payload = inputs[0].payload
        status = payload.get("status")
        return TaskResultMessage(
            task_id=str(payload["task_id"]), task_name=str(payload["task_name"]),
            status=(TaskResultStatus.CANCELLED if status == TaskStatus.STOPPED else
                    TaskResultStatus.COMPLETED if status == TaskStatus.COMPLETED else
                    TaskResultStatus.ORPHANED if status == TaskStatus.ORPHANED else TaskResultStatus.FAILED),
            result=_task_notice_text(payload),
        )
    # 2. 多条通知合并成一条消息；状态相同则保留该状态，不同时用 mixed，按完整条目分批保留全部日志引用。
    statuses = {item.payload.get("status") for item in inputs}
    mapped_statuses = {
        TaskResultStatus.CANCELLED if status == TaskStatus.STOPPED else
        TaskResultStatus.COMPLETED if status == TaskStatus.COMPLETED else
        TaskResultStatus.ORPHANED if status == TaskStatus.ORPHANED else TaskResultStatus.FAILED
        for status in statuses
    }
    combined_status = mapped_statuses.pop() if len(mapped_statuses) == 1 else TaskResultStatus.MIXED
    details = "\n".join(
        f"{item.payload['task_id']} [{item.payload.get('status')}]: "
        f"{_task_notice_text(item.payload)}"
        for item in inputs
    )
    return TaskResultMessage(
        task_id="batch:" + ",".join(str(item.payload["task_id"]) for item in inputs),
        task_name=f"{len(inputs)} 个后台任务",
        status=combined_status,
        result=details,
    )


def _task_notice_text(payload: dict) -> str:
    """格式化新通知元信息，同时保留重启前旧摘要通知的读取兼容。

    Args:
        payload (dict): 从持久 Inbox 读取的一条任务通知。

    Returns:
        str: 新通知只包含工具、退出码和日志引用，旧通知保留既有摘要。
    """
    if payload.get("output_ref"):
        return (f"tool_name: {payload.get('tool_name') or '未知工具'}\n"
                f"exit_code: {payload.get('exit_code')}\n"
                f"output_ref: {payload['output_ref']}")
    return str(payload.get("result") or "")


def task_result_messages(inputs: tuple[InboxEnvelope, ...]) -> tuple[TaskResultMessage, ...]:
    """按完整任务条目拆批，所有引用都进入消息，不截断后面的任务。

    Args:
        inputs (tuple[InboxEnvelope, ...]): 本次已查询的全部终态通知。

    Returns:
        tuple[TaskResultMessage, ...]: 按原顺序分组的全部结果消息。
    """
    batches = []
    group = []
    size = 0
    for input_record in inputs:
        item_size = len(_task_notice_text(input_record.payload)) + len(str(input_record.payload["task_id"])) + 64
        if group and size + item_size > TASK_NOTICE_BATCH_MAX_CHARS:
            batches.append(_task_result_message(tuple(group)))
            group, size = [], 0
        group.append(input_record)
        size += item_size
    if group:
        batches.append(_task_result_message(tuple(group)))
    return tuple(batches)


class SessionStateManager:
    """提供会话输入受理、状态提交与恢复业务，使用后由拥有方关闭。

    worker 拥有其 Manager，Agent 和 Pipeline 只借用；独立 Agent 拥有默认创建的
    Manager。命令处理另建短期对象。当前执行状态始终由调用方持有，不在本类缓存。

    Attributes:
        session_id (str): 当前业务操作所属的会话编号。
        db_path (str): 两个 DAO 使用的同一 SQLite 文件路径。
        _state_dao (AgentStateDao): checkpoint 与协议历史的事务存储。
        _inbox_dao (SessionInboxDao): 持久输入的查询及条件写入。
    """

    def __init__(self, session_id: str, db_path: str | None = None, *,
                 state_dao: AgentStateDao | None = None, inbox_dao: SessionInboxDao | None = None) -> None:
        """绑定当前会话的两个具体存储对象，延迟打开数据库连接。

        Args:
            session_id (str): 当前会话编号。
            db_path (str | None): 数据库路径；省略时由环境选择。
            state_dao (AgentStateDao | None): 可注入的状态存储，由本对象负责关闭。
            inbox_dao (SessionInboxDao | None): 可注入的输入存储，由本对象负责关闭。
        """
        self.session_id = session_id
        self._state_dao = state_dao or AgentStateDao(session_id, db_path)
        self.db_path = self._state_dao.db_path
        self._inbox_dao = inbox_dao or SessionInboxDao(session_id, self.db_path)

    @property
    def last_loaded_checkpoint_type(self) -> CheckpointType | None:
        """返回最近一次读取的快照类型，不缓存或另存执行状态。

        Returns:
            CheckpointType | None: 最近读到的类型，没有持久快照时为空。
        """
        return self._state_dao.last_loaded_checkpoint_type

    async def load(self) -> AgentState:
        """加载业务操作所需状态，没有 checkpoint 时创建空内存状态。

        Returns:
            AgentState: 本次执行使用的唯一状态对象。
        """
        state = await self._state_dao.load()
        return state if state is not None else AgentState.create_new(self.session_id)

    async def load_for_resume(self, request_id: str) -> AgentState | None:
        """核对持久中断编号及审批决定，交给 Agent 恢复原工具调用。

        Args:
            request_id (str): 本次要求恢复的中断编号。

        Returns:
            AgentState | None: 编号匹配且具备恢复材料的状态；请求过期时为空。
        """
        state = await self.load()
        interrupt = state.interrupt_data or {}
        if interrupt.get("request_id") != request_id:
            return None
        if interrupt.get("type") == InterruptKind.AUTHORIZATION_REQUEST and not interrupt.get("runtime_decision"):
            return None
        return state

    async def save(self, state: AgentState, *, checkpoint_type: CheckpointType,
                   inbox_inputs: tuple[InboxEnvelope, ...] = (), rewritten_message_id: str | None = None,
                   release_queued: bool = False) -> int:
        """先准备媒体，再原子保存状态、历史和本次输入交付。

        Args:
            state (AgentState): 调用方正在使用的同一个会话状态。
            checkpoint_type (CheckpointType): 保存中间执行进度或已结束轮次。
            inbox_inputs (tuple[InboxEnvelope, ...]): 本次随消息一起交付的输入。
            rewritten_message_id (str | None): 需要保留身份并重写的助手消息。
            release_queued (bool): 调用方确认正常结束后要求的排队放行。

        Returns:
            int: 已原子提交的 checkpoint 编号。
        """
        # 1. 图片文件先准备；文件不属于 SQLite 回滚范围，失败规则保持原样。
        await asyncio.to_thread(self._externalize_images, state)
        # 2. 一个 DAO 事务完成跨表提交，Inbox 不另开独立提交。
        return await self._state_dao.save(state, checkpoint_type=checkpoint_type,
            inbox_inputs=inbox_inputs, rewritten_message_id=rewritten_message_id, release_queued=release_queued)

    async def save_progress(self, state: AgentState) -> int:
        """由 Pipeline 在模型或工具安全点保存中间执行进度。

        Args:
            state (AgentState): 当前执行状态。

        Returns:
            int: 已保存的 checkpoint 编号。
        """
        return await self.save(state, checkpoint_type="intermediate")

    async def complete_turn(self, state: AgentState, *, release_queued: bool = False) -> int:
        """按 Agent 的最终提交决定保存完成状态，不运行响应钩子。

        Args:
            state (AgentState): 当前轮次的完整状态。
            release_queued (bool): 正常完成真人轮次时同时放行排队输入。

        Returns:
            int: 已保存的 checkpoint 编号。
        """
        # 1. 完成状态与背景清空一起提交，下一轮只读取新的临时内容。
        state.clear_temporary_context()
        return await self.save(state, checkpoint_type="completed", release_queued=release_queued)

    async def save_interrupt_origin(self, state: AgentState, origin_channel: str) -> None:
        """发布中断前补充原回答来源，已经保存来源的请求不重复提交。

        Args:
            state (AgentState): Pipeline 刚刚保存的中断状态。
            origin_channel (str): 当前回答的实际来源渠道。
        """
        if state.interrupt_data and "origin_channel" not in state.interrupt_data:
            # 1. 保留恢复材料和剩余调用，只补充重连及恢复时需要的来源。
            state.set_interrupt({**state.interrupt_data, "origin_channel": origin_channel}, state.pending_actions)
            # 2. 保存成功后 Agent 才转出中断事件，保持审批的原有可见顺序。
            await self.save_progress(state)

    async def close_previous_tools(self, state: AgentState) -> None:
        """新真人输入提交前结束旧工具协议，避免把两个轮次的调用混在一起。

        Args:
            state (AgentState): 新激活前加载的状态。
        """
        if state.close_unfinished_tools(USER_REJECTED_INTERRUPTED_TOOL_RESULT,
                USER_CANCELLED_PENDING_TOOL_RESULT, INTERRUPTED_TOOL_RESULT):
            await self.complete_turn(state)

    async def commit_human_input(self, state: AgentState, message: str, images: list[str] | None, *,
                                 origin_channel: str = DESKTOP_CHANNEL,
                                 inputs: tuple[InboxEnvelope, ...] = ()) -> HumanMessage:
        """提交同批任务通知和一条真人消息，成功后返回可报告的持久真人事实。

        Args:
            state (AgentState): 当前执行的同一状态对象。
            message (str): 真人正文。
            images (list[str] | None): 真人附件地址。
            origin_channel (str): 本次真人输入来源。
            inputs (tuple[InboxEnvelope, ...]): 与本次消息原子交付的 Inbox 记录。

        Returns:
            HumanMessage: 已经保存的真人消息；失败时不返回消息。
        """
        # 1. 同批任务通知先进入会话，不推进真人计数。
        for notice in task_result_messages(tuple(item for item in inputs if item.kind == InboxKind.TASK_TERMINAL)):
            state.append_input(notice)
        filenames = []
        if images:
            try:
                filenames = await asyncio.to_thread(save_multiple_images, images)
            except Exception as exc:
                logger.warning("[SessionStateManager] 用户图片保存失败: %s", exc)
        human = state.add_user_message(message, image_urls=images or (), image_filenames=filenames,
                                       origin_channel=origin_channel)
        # 2. 计数与消息同事务保存；提交后的模型失败不撤销真人事实。
        state.increment_long_term_memory_turn_counter()
        await self.save(state, checkpoint_type="intermediate", inbox_inputs=inputs)
        return human

    async def commit_runtime_inputs(self, state: AgentState, messages: tuple[RuntimeInputMessage, ...] | None,
                                    inputs: tuple[InboxEnvelope, ...]) -> None:
        """提交显式内部消息，或转换当前 Inbox 任务通知，不触发激活钩子。

        Args:
            state (AgentState): 当前执行状态。
            messages (tuple[RuntimeInputMessage, ...] | None): 显式消息；为空值时转换任务记录。
            inputs (tuple[InboxEnvelope, ...]): 本次需要交付的输入记录。
        """
        # 1. 显式消息与任务转换互斥，防止同批通知重复追加。
        for message in task_result_messages(inputs) if messages is None else messages:
            state.append_input(message)
        # 2. 内部消息不增加真人计数，原子交付成功后调用方才继续执行。
        await self.save(state, checkpoint_type="intermediate", inbox_inputs=inputs)

    async def deliver_pending(self, state: AgentState) -> None:
        """由 Pipeline 在工具协议闭合、下一模型请求前交付已到达的任务通知。

        Args:
            state (AgentState): 即将继续模型调用的执行状态。
        """
        inputs = await self._inbox_dao.read_task_terminals()
        if inputs:
            await self.commit_runtime_inputs(state, None, inputs)

    async def record_authorization_decision(self, request_id: str | None, decision: dict,
                                            actor_channel: str = DESKTOP_CHANNEL) -> str | None:
        """记录当前请求的首次审批决定，成功后返回原回答来源供 Scheduler 发布事件。

        Args:
            request_id (str | None): 用户处理的审批编号。
            decision (dict): 批准、拒绝或引导决定。
            actor_channel (str): 引导消息实际来源。

        Returns:
            str | None: 成功时的原回答来源；请求已过期或已处理时为空。
        """
        state = await self.load()
        interrupt = state.get_pending_authorization(request_id)
        if interrupt is None:
            return None
        # 1. 引导正文沿用现有独立受理事务，失败不能关闭审批。
        if decision.get("kind") == "steer":
            await self.submit_human(actor_channel, decision["message"])
        if not state.record_authorization_decision(request_id, decision):
            return None
        # 2. 决定属于 checkpoint，不是 Inbox 新消息；保存成功才报告受理。
        await self.save_progress(state)
        return str(interrupt.get("origin_channel") or DESKTOP_CHANNEL)

    async def cancel_pending_interrupt(self) -> tuple[str, str] | None:
        """为已受理的新消息取消旧审批，返回原请求编号及来源。

        Returns:
            tuple[str, str] | None: 已保存的取消请求编号及来源；没有未决请求时为空。
        """
        state = await self.load()
        interrupt = state.interrupt_data or {}
        if not state.cancel_pending_interrupt():
            return None
        await self.save_progress(state)
        return str(interrupt.get("request_id") or ""), str(interrupt.get("origin_channel") or DESKTOP_CHANNEL)

    async def finish_interrupted_turn(self, state: AgentState, *, partial: str, partial_persisted: bool,
                                      origin_channel: str) -> None:
        """保存用户打断后的真实片段并闭合工具协议，不放行排队输入。

        Args:
            state (AgentState): 当前执行状态。
            partial (str): 本模型步骤已显示的正文。
            partial_persisted (bool): 该步骤是否已记录为助手消息。
            origin_channel (str): 该正文所属渠道。
        """
        cancelled = "用户打断，本次工具调用已取消。"
        state.close_unfinished_tools(cancelled, cancelled, cancelled)
        rewritten_message_id = None
        # 1. 已保存片段沿用原消息身份；未保存片段新增一次，不重复正文。
        if partial.strip() and not partial_persisted:
            state.add_assistant_message(partial + "……（被用户打断）", origin_channel=origin_channel)
        elif partial_persisted and partial.strip():
            previous = next((item for item in reversed(state.messages) if isinstance(item, AssistantMessage)
                and item.text == partial and item.origin_channel == origin_channel), None)
            if previous is None:
                raise RuntimeError("已保存的流式文本缺少对应 assistant 消息")
            state.replace_message(replace(previous, content=text_content(previous.text + "……（被用户打断）")))
            rewritten_message_id = previous.message_id
        # 2. 终止轮次清空背景并保存 completed，不放行队列。
        state.clear_temporary_context()
        await self.save(state, checkpoint_type="completed", rewritten_message_id=rewritten_message_id)

    async def unfinished_sessions(self) -> list[str]:
        """查询需要无执行收尾的会话，查询本身不启动 Agent。

        Returns:
            list[str]: 最新 checkpoint 未完成或工具协议未闭合的会话。
        """
        return [session for session, state, kind in await self._state_dao.latest_checkpoints()
                if kind == "intermediate" or state.get("interrupt_data") or state.get("pending_tool_calls")]

    async def reset_after_restart(self) -> None:
        """按现有启动关闭规则作废 pending 真人意图，保留任务通知和交付事实。"""
        await self._inbox_dao.reset_after_restart(self.db_path)

    async def clear_session(self) -> int:
        """删除当前会话 checkpoint，保留完整消息历史。

        Returns:
            int: 删除的 checkpoint 数量。
        """
        return await self._state_dao.clear_session()

    async def close(self) -> None:
        """关闭两个自有 DAO，前一个关闭失败时仍尝试关闭另一个。"""
        try:
            await self._state_dao.close()
        finally:
            await self._inbox_dao.close()

    async def submit_human(self, origin_channel: str, message: str, *, images: list[str] | None = None, queued: bool = False) -> InboxEnvelope:
        """持久受理真人输入，返回 pending 输入快照。

        Args:
            origin_channel (str): 实际发送真人消息的渠道。
            message (str): 用户发送的正文，允许与纯图片附件一起受理。
            images (list[str] | None): 用户附带的图片地址。
            queued (bool): 是否等待正常真人轮次完成后再允许交付。

        Returns:
            InboxEnvelope: 本次操作的持久结果。
        """
        return await self._inbox_dao.submit_human(origin_channel, message, images=images, queued=queued)

    async def read_activation_batch(self, *, kind: InboxKind | str | None = None, include_queued: bool = True) -> tuple[InboxEnvelope, ...]:
        """为 worker 查询候选输入，不领取或交付。

        Args:
            kind (InboxKind | str | None): 审批等待期间可限定只查询真人输入。
            include_queued (bool): 是否包括已经放行的排队输入。

        Returns:
            tuple[InboxEnvelope, ...]: 本次操作的持久结果。
        """
        return await self._inbox_dao.read_activation_batch(kind=kind, include_queued=include_queued)

    async def read_next(self, *, kind: InboxKind | str | None = None, include_queued: bool = True) -> InboxEnvelope | None:
        """查询最早的可交付输入。

        Args:
            kind (InboxKind | str | None): 需要查询的输入类别；为空时查询全部类别。
            include_queued (bool): 是否包括已经放行的排队输入。

        Returns:
            InboxEnvelope | None: 本次操作的持久结果。
        """
        return await self._inbox_dao.read_next(kind=kind, include_queued=include_queued)

    async def read_task_terminals(self) -> tuple[InboxEnvelope, ...]:
        """查询尚未交付的任务终态通知。

        Returns:
            tuple[InboxEnvelope, ...]: 本次操作的持久结果。
        """
        return await self._inbox_dao.read_task_terminals()

    async def pending_humans(self, *, origin_channel: str | None = None) -> list[InboxEnvelope]:
        """查询当前会话尚未交付的真人输入。

        Args:
            origin_channel (str | None): 按需限定发送来源；为空时查询所有来源。

        Returns:
            list[InboxEnvelope]: 本次操作的持久结果。
        """
        return await self._inbox_dao.pending_humans(origin_channel=origin_channel)

    async def cancel_queued(self, envelope_id: int, origin_channel: str) -> bool:
        """按来源条件撤销尚未交付的排队输入。

        Args:
            envelope_id (int): 要撤销的持久输入编号。
            origin_channel (str): 发起撤销的来源，只允许撤销同来源输入。

        Returns:
            bool: 本次操作的持久结果。
        """
        return await self._inbox_dao.cancel_queued(envelope_id, origin_channel)

    async def status(self, envelope_id: int) -> InboxStatus | None:
        """查询输入交付事实，供 Scheduler 判断失败后是否可以重试。

        Args:
            envelope_id (int): 需要核对 pending 或 delivered 的输入编号。

        Returns:
            InboxStatus | None: 本次操作的持久结果。
        """
        return await self._inbox_dao.status(envelope_id)

    async def discard_human_input(self, envelope_id: int) -> None:
        """重试耗尽后移除当前未交付真人输入。

        Args:
            envelope_id (int): 本轮重试耗尽的真人输入编号。
        """
        return await self._inbox_dao.discard_human_input(envelope_id)

    async def release_queued(self) -> None:
        """按调用方决定独立放行排队输入。"""
        return await self._inbox_dao.release_queued()

    @classmethod
    def _externalize_images(cls, state: AgentState) -> None:
        """按图片块保存本地文件；失败保留原地址，不丢失完整工具结果。

        Args:
            state (AgentState): 需要保存、恢复或投影的会话业务状态。
        """
        # 1. 已压缩图片不能重载；真人附件已经由输入入口保存，不重复落盘。
        for message in state.messages:
            if not isinstance(message, ToolMessage):
                continue
            groups: dict[str, list[tuple[int, ImageContent]]] = {}
            for index, part in enumerate(message.content):
                if isinstance(part, ImageContent) and part.context_state == "available" and not part.storage_ref:
                    groups.setdefault(part.media_kind, []).append((index, part))
            parts = list(message.content)
            changed = False
            for kind, group in groups.items():
                try:
                    # 1.1. 同类别图片保持原有成组落盘与失败清理规则，不写入一半引用。
                    filenames = save_multiple_images([part.url for _, part in group], category=kind)
                except Exception as exc:
                    logger.warning("[SessionStateManager] 图片外置失败，保留原地址: %s", exc)
                    filenames = []
                if len(filenames) == len(group):
                    for (index, part), filename in zip(group, filenames, strict=True):
                        parts[index] = replace(part, storage_ref=filename)
                    changed = True
            # 2. 保存只补文件引用，内存及 checkpoint 的可用图片地址保持原样。
            if changed:
                state.replace_message(replace(message, content=tuple(parts)))


    async def finalize_abandoned_turn(
        self, *, partial: str = "", partial_persisted: bool = False,
        origin_channel: str = DESKTOP_CHANNEL, partial_message_id: str | None = None,
    ) -> bool:
        """只加载和持久结束旧轮次，不创建执行对象或运行模型、工具与插件。

        Args:
            partial (str): 正常关闭前捕获的当前模型步骤可见片段。
            partial_persisted (bool): 片段是否已记录到内存 Assistant 步骤，仍需核对落盘状态。
            origin_channel (str): 片段所属渠道。
            partial_message_id (str | None): 当前步骤的消息身份，避免匹配到以前相同正文。

        Returns:
            bool: 是否提交了本次终止；已完成状态重复调用不再写入。

        Raises:
            Exception: 持久保存失败，原 checkpoint 和协议记录保持不变。
        """
        state = await self.load()
        if self._state_dao.last_loaded_checkpoint_type != "intermediate" and not (
            state.interrupt_data or state.pending_tool_calls
        ):
            return False
        # 1. 使用持久回报形成事实记录，恢复不经过工具执行或 VLM。
        interrupt = state.interrupt_data or {}
        receipt = interrupt.get("desktop_result")
        content = APPLICATION_EXIT_RESULT
        images: tuple[str, ...] = ()
        if receipt is not None:
            screenshot = receipt.get("screenshot_data")
            images = (screenshot,) if isinstance(screenshot, str) and screenshot else ()
            facts = {key: value for key, value in receipt.items() if key != "screenshot_data"}
            content = "应用退出，本轮已终止；已保存桌面执行回报：" + json.dumps(facts, ensure_ascii=False)
        elif (interrupt.get("resume_state") or {}).get("continuation_claimed"):
            content = APPLICATION_EXIT_UNKNOWN_RESULT
        state.close_unfinished_tools(content, APPLICATION_EXIT_RESULT, APPLICATION_EXIT_UNKNOWN_RESULT,
                                     interrupted_images=images)
        # 2. 只保留捕获到的真实片段；已持久文本沿用原消息身份，不复制。
        rewritten_message_id = None
        if partial.strip():
            suffix = "……（应用退出，本轮已终止）"
            previous = None
            if partial_message_id is not None:
                previous = next((item for item in state.messages
                                 if isinstance(item, AssistantMessage)
                                 and item.message_id == partial_message_id), None)
            elif partial_persisted:
                previous = next((item for item in reversed(state.messages)
                                 if isinstance(item, AssistantMessage) and item.text == partial
                                 and item.origin_channel == origin_channel), None)
            if previous is not None:
                state.replace_message(with_text(previous, previous.text + suffix))
                rewritten_message_id = previous.message_id
            else:
                # 2.1. 最终提交或工具前 checkpoint 被取消时，内存步骤尚未落盘，需补保存。
                state.add_assistant_message(partial + suffix, origin_channel=origin_channel)
        # 3. 清空已结束轮次的背景，与终止状态一起保存，不运行响应钩子或放行队列。
        state.clear_temporary_context()
        await self.save(state, checkpoint_type="completed", rewritten_message_id=rewritten_message_id)
        return True
