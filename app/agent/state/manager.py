"""保存会话输入和执行进度，处理工具审批、回答中断及应用重启后的整理。"""

from __future__ import annotations
from app.schemas.message_types import MediaKind

import asyncio
from dataclasses import replace
import logging

from app.schemas.runtime_types import CheckpointType, DESKTOP_CHANNEL, InboxKind, InboxStatus, InterruptKind, TaskResultStatus, TaskStatus, TurnInterruptReason
from app.agent.runtime.session_inbox import InboxEnvelope, InputNoLongerPending
from app.agent.state.model import AgentState
from app.agent.state.messages import (
    AssistantMessage, ContentPart, HumanMessage, ImageContent, RuntimeInputMessage, ToolMessage,
    TaskResultMessage, PluginNotificationMessage, SubagentMessage, text_content, with_text,
)
from app.agent.utils.domain.images import save_multiple_images
from app.crud.sqlite_factory import SqliteFactory
from app.crud.agent_state_dao import AgentStateDao
from app.crud.session_inbox_dao import SessionInboxDao
from app.schemas.agent_storage import AgentStorageScope

logger = logging.getLogger(__name__)

INTERRUPTED_TOOL_RESULT = "上次工具链中断，未获得该工具的可靠结果；为避免重复副作用，本次未自动重试。"
USER_REJECTED_INTERRUPTED_TOOL_RESULT = "用户发送了新的消息，待批准工具调用已拒绝。"
USER_CANCELLED_PENDING_TOOL_RESULT = "用户发送了新的消息，本批后续工具调用已取消。"
APPLICATION_EXIT_RESULT = "应用退出，本轮已终止；本次工具调用已取消。"
APPLICATION_EXIT_UNKNOWN_RESULT = "应用退出，本轮已终止；工具执行结果未知，本次不会自动重试。"
PARENT_MESSAGE_DELIVERED = "消息已投递。"
USER_STOPPED_TOOL_RESULT = "被用户取消，工具调用未完成"

TASK_NOTICE_BATCH_MAX_CHARS = 8192


#=====================将后台通知转换为对话消息======================

def _task_result_message(inputs: tuple[InboxEnvelope, ...]) -> TaskResultMessage:
    """将一组后台任务结果整理为一条加入 Agent 对话的任务消息。

    task_result_messages 已经按正文大小分好组；本函数保留组内任务的
    结果、错误和日志引用，供 Agent 在下一次模型请求中读取。

    Args:
        inputs (tuple[InboxEnvelope, ...]): 同一组非空任务通知，包含编号、名称、结束状态及结果。

    Returns:
        TaskResultMessage: 单个任务的结果消息，或保留组内各任务详情的合并消息。
    """
    # 1. 单条通知保留原任务身份，将执行状态转换为对话消息使用的状态。
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
    # 2. 多条通知合并展示；结束状态不同时使用 mixed，各任务的状态仍写在正文中。
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
    """生成 Agent 阅读的任务结果正文，终端任务提供完整输出的读取引用。

    Args:
        payload (dict): Inbox 中一条任务结束通知的内容。

    Returns:
        str: 普通任务的返回正文，或终端工具名、退出码和日志引用；有错误时附上错误摘要。
    """
    if payload.get("output_ref"):
        result = (f"tool_name: {payload.get('tool_name') or '未知工具'}\n"
                f"exit_code: {payload.get('exit_code')}\n"
                f"output_ref: {payload['output_ref']}")
    else:
        result = str(payload.get("result") or "")
    error = payload.get("error")
    return f"{result}\n错误：{error}".strip() if error else result


def task_result_messages(inputs: tuple[InboxEnvelope, ...]) -> tuple[TaskResultMessage, ...]:
    """将待处理任务通知分成多条对话消息，让 Agent 读取完整的任务结果。

    每组按估算的字符数控制大小；单个任务的信息完整保留，超大的单项独占一组。

    Args:
        inputs (tuple[InboxEnvelope, ...]): 按 Inbox 编号排列的任务结束通知。

    Returns:
        tuple[TaskResultMessage, ...]: 按原通知顺序生成的消息，空输入返回空元组。
    """
    batches = []
    group = []
    size = 0
    # 1. 逐项估算正文大小，当前组加入下一任务会超出预算时，先输出已收集的整组。
    for input_record in inputs:
        item_size = len(_task_notice_text(input_record.payload)) + len(str(input_record.payload["task_id"])) + 64
        if group and size + item_size > TASK_NOTICE_BATCH_MAX_CHARS:
            batches.append(_task_result_message(tuple(group)))
            group, size = [], 0
        group.append(input_record)
        size += item_size
    # 2. 将最后一组转换为消息，包含最后一个任务的完整结果和日志引用。
    if group:
        batches.append(_task_result_message(tuple(group)))
    return tuple(batches)


def runtime_input_messages(inputs: tuple[InboxEnvelope, ...]) -> tuple[HumanMessage | RuntimeInputMessage, ...]:
    """把同批父指令、任务结果和插件通知转换为加入 Agent 对话的消息。

    commit_human_input 和 commit_runtime_inputs 在保存输入时调用。
    父指令使用用户消息，来源和交互信息由 Inbox 保存。真人入口另行创建
    用户消息；这里保持同批父指令和通知的先后顺序。

    Args:
        inputs (tuple[InboxEnvelope, ...]): 本次准备加入对话的 Inbox 记录，可以同时包含真人输入。

    Returns:
        tuple[HumanMessage | RuntimeInputMessage, ...]: 父指令及内部通知生成的对话消息。
    """
    messages = []
    tasks = []
    # 1. 收集相邻任务结果；遇到插件通知时，先生成前面的任务消息，再追加插件消息。
    for item in inputs:
        if item.kind == InboxKind.TASK_TERMINAL:
            tasks.append(item)
        elif item.kind == InboxKind.PLUGIN_NOTIFICATION:
            messages.extend(task_result_messages(tuple(tasks)))
            tasks.clear()
            messages.append(PluginNotificationMessage(**item.payload))
        elif item.kind in {InboxKind.AGENT_COMMAND, InboxKind.SUBAGENT}:
            messages.extend(task_result_messages(tuple(tasks)))
            tasks.clear()
            if item.kind == InboxKind.AGENT_COMMAND:
                messages.append(HumanMessage(content=text_content(str(item.payload["text"])),
                                             origin_channel=item.origin_channel))
            else:
                messages.append(SubagentMessage(subagent_id=str(item.payload["subagent_id"]),
                                                 subagent_name=str(item.payload["subagent_name"]),
                                                 result=str(item.payload["text"])))
    # 2. 追加尚未转换的最后一组任务结果。
    messages.extend(task_result_messages(tuple(tasks)))
    return tuple(messages)


class RequiredSaveError(RuntimeError):
    """当前执行的必需持久化操作最终失败，执行所有者应恢复已提交上下文。"""


class SessionStateManager:
    """为一个会话保存待处理输入、对话消息和执行进度，并处理暂停后的恢复。

    Scheduler 用本类接收消息和保存审批决定；Agent 开始处理输入时加载
    AgentState，执行期间由 Agent、Pipeline 传入这个状态保存进度和结束结果。
    Inbox 的已交付标记表示输入已经随对话状态保存成功。

    父子会话持有并关闭各自的对象；独立 Agent 关闭自己创建的对象；
    Scheduler 启动整理时使用的短期对象，由启动流程关闭。

    Attributes:
        session_id (str): 当前输入、对话和执行状态所属的会话编号。
        db_path (str): 会话快照、对话历史和 Inbox 共用的 SQLite 文件路径。
        _state_dao (AgentStateDao): 保存和读取会话快照、对话历史，统一提交输入的已交付标记。
        _inbox_dao (SessionInboxDao): 保存、查询和撤销 Inbox 中的待处理输入。
        _input_completion_lock (asyncio.Lock): 排序真人输入保存与最终回答提交，让已受理队列随提交放行。
    """

    def __init__(self, session_id: str, sqlite_factory: SqliteFactory, *,
                 state_dao: AgentStateDao | None = None, inbox_dao: SessionInboxDao | None = None,
                 storage_scope: AgentStorageScope = AgentStorageScope.MAIN,
                 parent_session_id: str | None = None) -> None:
        """为当前会话准备快照和输入存储，数据库连接在实际读写时打开。

        Args:
            session_id (str): 当前会话编号。
            sqlite_factory (SqliteFactory): 应用提供的共享连接配置。
            state_dao (AgentStateDao | None): 状态与协议历史存储，省略时创建；由本对象关闭。
            inbox_dao (SessionInboxDao | None): 已创建的输入存储，省略时创建；由本对象关闭。
            storage_scope (AgentStorageScope): 固定主表或子表范围，与实际快照存储保持一致。
            parent_session_id (str | None): 子输入标记的固定父身份，父会话为空。
        """
        self.session_id = session_id
        self._parent_session_id = parent_session_id
        self.storage_scope = AgentStorageScope(storage_scope)
        self._state_dao = state_dao or AgentStateDao(session_id, sqlite_factory, storage_scope=self.storage_scope)
        self.db_path = self._state_dao.db_path
        self._inbox_dao = inbox_dao or SessionInboxDao(session_id, sqlite_factory, storage_scope=self.storage_scope)
        self._input_completion_lock = asyncio.Lock()
    #=====================接收消息和管理排队输入======================

    async def submit_human(self, origin_channel: str, message: str, *, images: list[str] | None = None,
                           queued: bool = False, discarded_ids: tuple[int, ...] = ()) -> InboxEnvelope:
        """用户发送消息时将正文和附件存入 Inbox，等待会话开始处理。

        Scheduler 在保存成功后安排执行，或等待当前轮正常结束后处理排队消息。
        输入加入对话和真人计数的更新由 commit_human_input 在执行开始时完成。

        Args:
            origin_channel (str): 用户发送消息的界面或渠道。
            message (str): 用户正文；仅发送图片时可以为空。
            images (list[str] | None): 用户附件的图片地址。
            queued (bool): 是否等待当前真人轮次正常结束后再允许处理。

            discarded_ids (tuple[int, ...]): 前次交付失败且由本会话明确放弃的输入编号。

        Returns:
            InboxEnvelope: 已存入数据库的待处理输入，包含编号、来源、内容和排队方式。
        """
        async with self._input_completion_lock:
            return await self._inbox_dao.submit_human(origin_channel, message, images=images, queued=queued, discarded_ids=discarded_ids)

    async def submit_plugin_notification(self, plugin_name: str, name: str, text: str) -> None:
        """Scheduler 处理插件通知请求时，将通知存入 Inbox 等待 Agent 读取。

        保存成功后由 Scheduler 根据会话当前状态安排处理；消息在开始新轮次或
        现有轮次的下一次模型请求前加入对话。

        Args:
            plugin_name (str): 发出通知的插件名称。
            name (str): 通知用途的稳定名称，例如 battle_started。
            text (str): 需要 Agent 读取的通知正文。
        """
        await self._inbox_dao.submit_plugin_notification(plugin_name, name, text)

    async def pending_humans(self, *, origin_channel: str | None = None) -> list[InboxEnvelope]:
        """查询用户已经发送、尚未加入 Agent 对话的消息，供会话快照显示。

        Args:
            origin_channel (str | None): 限定用户消息的发送来源，省略时查询所有来源。

        Returns:
            list[InboxEnvelope]: 按输入编号排列的待处理真人消息，包含尚在排队的消息。
        """
        return await self._inbox_dao.pending_humans(origin_channel=origin_channel)

    async def cancel_queued(self, envelope_id: int, origin_channel: str) -> bool:
        """用户撤回排队消息时，尝试撤销同一来源尚未处理的那条输入。

        Args:
            envelope_id (int): 用户要撤回的 Inbox 输入编号。
            origin_channel (str): 发起撤回的界面或渠道，用于检查消息来源。

        Returns:
            bool: 输入属于该来源且仍可撤销时返回 True；已处理、已删除或不符合条件时返回 False。
        """
        return await self._inbox_dao.cancel_queued(envelope_id, origin_channel)

    async def status(self, envelope_id: int) -> InboxStatus | None:
        """处理输入失败后查询其保存结果，供 Scheduler 决定重试还是报告完成。

        Args:
            envelope_id (int): 本次执行尝试处理的 Inbox 输入编号。

        Returns:
            InboxStatus | None: pending 表示尚未随对话保存，delivered 表示已经保存；记录不存在时为空。
        """
        return await self._inbox_dao.status(envelope_id)

    async def discard_human_input(self, envelope_id: int) -> None:
        """用户输入处理重试耗尽时，删除仍未保存进对话的输入。

        Scheduler 调用后向界面报告该条消息处理失败，让用户决定是否重新发送。

        Args:
            envelope_id (int): 本轮重试耗尽的真人输入编号。
        """
        return await self._inbox_dao.discard_human_input(envelope_id)

    async def release_queued(self) -> None:
        """将当前会话的排队用户消息设为允许处理，后续由 worker 读取。

        这是独立提交的放行入口。正常回答结束时使用 complete_turn 的
        release_queued 参数，将放行与本轮结束状态一起保存。
        """
        return await self._inbox_dao.release_queued()

    #=====================开始一轮对话并保存输入======================

    async def read_activation_batch(self, *, kind: InboxKind | str | None = None, include_queued: bool = True) -> tuple[InboxEnvelope, ...]:
        """会话 worker 准备开始执行时，查询本次要处理的一批输入。

        本批包括符合条件的内部通知和最早的一条真人消息；查询后仍是 pending，
        Agent 将内容加入对话并保存成功时，才一起改为 delivered。

        Args:
            kind (InboxKind | str | None): 限定输入类别，等待审批期间可只查询真人消息。
            include_queued (bool): 是否包括已经获准开始处理的排队消息。

        Returns:
            tuple[InboxEnvelope, ...]: 按 Inbox 编号排列的本批输入，没有符合条件的输入时为空元组。
        """
        return await self._inbox_dao.read_activation_batch(kind=kind, include_queued=include_queued)

    async def read_next(self, *, kind: InboxKind | str | None = None, include_queued: bool = True) -> InboxEnvelope | None:
        """查询最早的一条可处理输入，供需要逐条读取的调用方使用。

        Args:
            kind (InboxKind | str | None): 限定输入类别，省略时查询所有类别。
            include_queued (bool): 是否包括已经获准开始处理的排队消息。

        Returns:
            InboxEnvelope | None: 最早的待处理输入，没有符合条件的记录时为空；读取保持 pending 状态。
        """
        return await self._inbox_dao.read_next(kind=kind, include_queued=include_queued)

    @property
    def last_loaded_checkpoint_type(self) -> CheckpointType | None:
        """读取最近一次加载的会话快照类型，供调用方判断上轮是否已经结束。

        Returns:
            CheckpointType | None: intermediate 表示执行进度，completed 表示轮次已结束；没有快照时为空。
        """
        return self._state_dao.last_loaded_checkpoint_type

    async def load(self) -> AgentState:
        """Agent 开始一轮对话或 Scheduler 处理会话命令时，加载最新会话状态。

        Returns:
            AgentState: 从最新 checkpoint 恢复的状态；首次使用该会话时返回新的空状态。
        """
        state = await self._state_dao.load()
        return state if state is not None else AgentState.create_new(self.session_id)

    async def committed_history_position(self) -> str | None:
        """读取最近已保存历史位置，供故障恢复事件限定刷新范围。"""
        return await self._state_dao.committed_history_position()

    async def close_previous_tools(self, state: AgentState) -> None:
        """Agent 开始处理新用户消息前，为上轮未结束的工具调用补上结束结果。

        旧轮可能停在审批，也可能留下尚无结果的工具调用。根据其状态记录拒绝、
        取消或结果未知的消息，再保存旧轮已结束状态，随后才能加入本轮输入。

        Args:
            state (AgentState): 新用户消息加入前加载的会话状态，可能包含上轮遗留的工具调用。
        """
        # 1. 根据旧轮工具的执行情况补齐结果；确有修改时保存旧轮结束状态。
        interrupted_result = (PARENT_MESSAGE_DELIVERED if (state.interrupt_data or {}).get("type") == InterruptKind.PARENT_MESSAGE
                              else USER_REJECTED_INTERRUPTED_TOOL_RESULT)
        if state.close_unfinished_tools(interrupted_result,
                USER_CANCELLED_PENDING_TOOL_RESULT, INTERRUPTED_TOOL_RESULT):
            await self.complete_turn(state)

    async def commit_human_input(self, state: AgentState, message: str, images: list[str] | None, *,
                                 origin_channel: str = DESKTOP_CHANNEL,
                                 inputs: tuple[InboxEnvelope, ...] = ()) -> HumanMessage:
        """Agent 开始处理用户消息时，将同批通知和真人输入加入对话并保存。

        Agent 已完成输入保存前的插件钩子。本方法先追加内部通知，再追加真人消息、
        图片和真人计数；对话历史、执行快照及 Inbox 已交付标记在同一事务保存。
        返回后，Agent 才报告真人消息保存成功并进入后续插件钩子和模型执行。

        Args:
            state (AgentState): 已加载并整理完上轮工具调用的当前会话状态。
            message (str): 本轮用户正文。
            images (list[str] | None): 本轮用户附件的图片地址。
            origin_channel (str): 用户消息的实际来源渠道。
            inputs (tuple[InboxEnvelope, ...]): 随本次对话保存一起标记为已交付的 Inbox 记录。

        Returns:
            HumanMessage: 已保存的真人消息，供 Agent 使用其编号和正文通知界面显示。

        Raises:
            Exception: 输入已失效或数据库保存失败；本次数据库事务回滚，错误交给执行调用方处理。
        """
        # 1. 先把同批任务结果和插件通知加入对话，这些内部消息不增加真人计数。
        for notice in runtime_input_messages(inputs):
            state.append_input(notice)
        # 2. 将真人附件加入本轮输入，由统一保存入口落盘并提交执行进度。
        human = state.add_user_message(message, image_urls=images or (),
                                       origin_channel=origin_channel)
        state.increment_long_term_memory_turn_counter()
        await self.save(state, checkpoint_type=CheckpointType.INTERMEDIATE, inbox_inputs=inputs)
        return next(item for item in state.messages
                    if isinstance(item, HumanMessage) and item.message_id == human.message_id)

    async def commit_runtime_inputs(self, state: AgentState, messages: tuple[HumanMessage | RuntimeInputMessage, ...] | None,
                                    inputs: tuple[InboxEnvelope, ...]) -> None:
        """将父指令或内部通知加入当前对话，并保存输入已经交给 Agent 的记录。

        Agent 由父指令或内部通知启动新轮次时，以及 Pipeline 在下一次模型请求前
        追加通知时，都会使用此方法。输入保存执行进度，真人计数由真人入口维护。

        Args:
            state (AgentState): 需要加入父指令或内部通知的当前会话状态。
            messages (tuple[HumanMessage | RuntimeInputMessage, ...] | None): 调用方已构造的消息；为 None 时从 inputs 转换。
            inputs (tuple[InboxEnvelope, ...]): 需要随本次对话保存标记为已交付的 Inbox 记录。
        """
        # 1. 使用调用方传入的消息，或将 Inbox 通知转换成消息，再按顺序加入对话。
        for message in runtime_input_messages(inputs) if messages is None else messages:
            state.append_input(message)
        # 2. 同时保存新增消息、执行快照和通知的已交付标记，成功后再继续模型调用。
        await self.save(state, checkpoint_type=CheckpointType.INTERMEDIATE, inbox_inputs=inputs)

    #=====================执行期间保存进度和追加通知======================

    async def save_progress(self, state: AgentState) -> int:
        """执行期间保存当前对话、工具进度和等待信息，供后续继续处理。

        Pipeline 在工具调用前后或进入等待时保存；审批命令也用它保存用户决定。

        Args:
            state (AgentState): 调用方刚更新的会话状态。

        Returns:
            int: 类型为 intermediate 的已保存执行快照编号。
        """
        return await self.save(state, checkpoint_type=CheckpointType.INTERMEDIATE)

    async def deliver_pending(self, state: AgentState) -> None:
        """Pipeline 准备下一次模型请求时，把新到达的任务结果和插件通知加入对话。

        调用位置已经处理完上一批工具调用及结果。有新通知时统一保存消息和
        已交付标记，随后这次模型请求就能读取通知正文。

        Args:
            state (AgentState): 即将用于下一次模型请求的会话状态。
        """
        # 1. 查询当前待处理的内部通知，有内容时加入对话并保存。
        inputs = await self._inbox_dao.read_runtime_inputs()
        if inputs:
            await self.commit_runtime_inputs(state, None, inputs)

    async def read_task_terminals(self) -> tuple[InboxEnvelope, ...]:
        """单独查询当前会话尚未加入 Agent 对话的后台任务结束通知。

        该查询只返回任务结果；执行期间同时接收任务结果和插件通知使用 deliver_pending。

        Returns:
            tuple[InboxEnvelope, ...]: 按 Inbox 编号排列的待处理任务通知，查询后仍保持 pending 状态。
        """
        return await self._inbox_dao.read_task_terminals()

    #=====================等待工具审批和恢复执行======================

    async def commit_parent_interrupt(self, state: AgentState) -> bool:
        """准备图片后保存子暂停及父问题，归属和废弃事实由同笔事务校验。

        Args:
            state (AgentState): 本次准备保存的完整会话状态。
        """
        await asyncio.to_thread(self._externalize_images, state)
        try:
            return await self._state_dao.commit_parent_interrupt(state)
        except Exception as exc:
            raise RequiredSaveError(str(exc)) from exc

    async def record_authorization_decision(self, request_id: str | None, decision: dict,
                                            actor_channel: str = DESKTOP_CHANNEL) -> str | None:
        """保存用户的工具审批决定，供 Scheduler 恢复原调用或开始引导消息的新轮次。

        所属会话 已检查命令内容并串行处理当前会话的决定。这里加载等待状态，
        核对请求编号；引导正文与首次决定使用同一事务写入 Inbox 和执行快照。
        返回来源后，Scheduler 通知界面：批准、拒绝由 Agent 恢复原调用并处理决定，
        引导则结束旧轮次，再安排新消息开始执行。

        Args:
            request_id (str | None): 用户这次处理的审批请求编号，与当前等待请求比较。
            decision (dict): 已校验的批准、拒绝或引导决定，引导决定包含新正文。
            actor_channel (str): 用户作出决定的渠道，作为引导正文的消息来源。

        Returns:
            str | None: 决定保存成功后的原回答渠道；请求不匹配或已经记录决定时为空。
        """
        # 1. 读取当前等待的审批，只处理编号匹配且尚未作出决定的请求。
        state = await self.load()
        interrupt = state.get_pending_authorization(request_id)
        if interrupt is None:
            return None
        # 2. 准备首次决定，引导正文和决定在同一事务保存。
        if not state.record_authorization_decision(request_id, decision):
            return None
        saved = await self._state_dao.commit_interrupt_decision(state, origin_channel=actor_channel,
            parent_session_id=self._parent_session_id if self.storage_scope == AgentStorageScope.SUBAGENT else None)
        return str(interrupt.get("origin_channel") or DESKTOP_CHANNEL) if saved else None

    async def load_for_resume(self, request_id: str) -> AgentState | None:
        """Agent 恢复暂停的工具调用前，加载状态并检查这次恢复请求是否仍有效。

        请求编号必须与当前等待信息一致；等待人工审批的调用还必须已有用户决定。
        返回状态后，由 Agent、Pipeline 使用其中保存的工具参数和后续调用继续执行。

        Args:
            request_id (str): Scheduler 要求恢复的等待请求编号。

        Returns:
            AgentState | None: 编号匹配且审批条件满足的状态；编号过期或审批尚无决定时为空。
        """
        # 1. 加载最新快照，旧请求编号不能恢复已经被替换的等待调用。
        state = await self.load()
        interrupt = state.interrupt_data or {}
        if interrupt.get("request_id") != request_id:
            return None
        # 2. 人工审批须已保存决定；其他等待类型在这里完成编号检查后交给 Agent 处理。
        if interrupt.get("type") == InterruptKind.AUTHORIZATION_REQUEST and not interrupt.get("runtime_decision"):
            return None
        return state

    async def cancel_pending_interrupt(self) -> tuple[str, str] | None:
        """新用户消息已经保存时，取消当前仍在等待处理的旧工具请求。

        所属会话 在新输入替代审批或用户停止等待时调用。这里保存取消决定；
        返回旧请求编号和原回答渠道后，Session 发布已提交事件，再整理旧轮。

        Returns:
            tuple[str, str] | None: 已取消的请求编号和原回答渠道；没有等待请求或已有决定时为空。
        """
        # 1. 读取旧等待信息，尝试记录取消决定；已有决定时保持原处理结果。
        state = await self.load()
        interrupt = state.interrupt_data or {}
        if not state.cancel_pending_interrupt():
            return None
        # 2. 保存取消后的执行进度，再返回编号和渠道供 Session 发布取消结果。
        saved = await self._state_dao.commit_interrupt_decision(state, origin_channel=str(interrupt.get("origin_channel") or DESKTOP_CHANNEL))
        if not saved:
            return None
        return str(interrupt.get("request_id") or ""), str(interrupt.get("origin_channel") or DESKTOP_CHANNEL)

    #=====================正常结束和主动打断======================

    async def complete_turn(self, state: AgentState, *, release_queued: bool = False) -> int:
        """Agent 正常回答结束时，清除本轮临时背景并保存轮次已结束状态。

        Agent 在响应提交前后两个钩子之间调用；新消息开始前整理旧工具调用时，
        close_previous_tools 也使用此方法保存旧轮已经结束。

        Args:
            state (AgentState): 已记录本轮回答和工具结果的会话状态。
            release_queued (bool): 是否将排队消息的放行与本次结束状态一起保存。

        Returns:
            int: 类型为 completed 的已保存会话快照编号。
        """
        # 1. 已开始受理的排队输入先保存，最终回答随后原子放行这些输入。
        async with self._input_completion_lock:
            # 1.1. 本轮临时背景随结束状态一起清除，下轮重新准备自己的背景。
            state.clear_temporary_context()
            return await self.save(state, checkpoint_type=CheckpointType.COMPLETED, release_queued=release_queued)

    async def finish_interrupted_turn(self, state: AgentState, *, reason: TurnInterruptReason, partial: str, partial_persisted: bool,
                                      origin_channel: str) -> None:
        """当前轮次被停止或引导时，保存工具取消结果及已显示的回答片段。

        Agent 的执行中断和 Execution 的等待结束流程调用本方法。为未结束的工具调用补上取消结果，
        在本步骤的回答后添加打断说明，清除临时背景并保存本轮结束；排队消息
        继续等待后续明确的处理安排。

        Args:
            state (AgentState): 被打断的 Agent 正在使用的会话状态。
            reason (TurnInterruptReason): 已受理的停止或引导原因，决定未完成调用的结束正文。
            partial (str): 当前模型步骤已向界面显示的正文。
            partial_persisted (bool): 当前步骤是否已经加入内存中的助手消息，需要沿用其消息编号。
            origin_channel (str): 被打断回答所属的界面或渠道。

        Raises:
            RuntimeError: 调用方声明片段已经加入助手消息，但当前状态中找不到对应消息。
        """
        # 1. 为当前尚未结束的工具调用追加取消结果，保留前面已经完成的工具结果。
        cancelled = USER_STOPPED_TOOL_RESULT if reason == TurnInterruptReason.STOP else "用户打断，本次工具调用已取消。"
        interrupted_result = (PARENT_MESSAGE_DELIVERED if (state.interrupt_data or {}).get("type") == InterruptKind.PARENT_MESSAGE else cancelled)
        state.close_unfinished_tools(interrupted_result, cancelled, cancelled)
        rewritten_message_id = None
        # 2. 片段尚未加入会话时新增消息；已经加入时替换原消息并保留其编号。
        if partial.strip() and not partial_persisted:
            state.add_assistant_message(partial + "……（被用户打断）", origin_channel=origin_channel)
        elif partial_persisted and partial.strip():
            # 2.1. 按当前步骤正文和来源找到原助手消息，缺失时报告状态不一致。
            previous = next((item for item in reversed(state.messages) if isinstance(item, AssistantMessage)
                and item.text == partial and item.origin_channel == origin_channel), None)
            if previous is None:
                raise RuntimeError("已保存的流式文本缺少对应 assistant 消息")
            state.replace_message(replace(previous, content=text_content(previous.text + "……（被用户打断）")))
            rewritten_message_id = previous.message_id
        # 3. 清除本轮背景，保存工具取消结果和回答片段；已有助手消息同步重写历史记录。
        state.clear_temporary_context()
        await self.save(state, checkpoint_type=CheckpointType.COMPLETED, rewritten_message_id=rewritten_message_id)

    async def finish_failed_turn(self, state: AgentState, *, error: str, partial: str = "",
                                 origin_channel: str = DESKTOP_CHANNEL) -> None:
        """保存执行失败说明并补齐旧工具协议，使下次明确输入从已结束的轮次继续。

        Args:
            state (AgentState): 本次失败前仍可信的会话状态。
            error (str): 模型或业务执行的最终失败原因。
            partial (str): 已展示且尚未保存的本步骤正文。
            origin_channel (str): 失败回答所属渠道。
        """
        # 1. 已完成的工具结果保留，尚未完成的调用统一记录本次执行失败。
        detail = f"本轮执行失败：{error}"
        state.close_unfinished_tools(detail, detail, detail)
        state.clear_temporary_context()
        state.add_assistant_message(f"{partial}\n\n{detail}" if partial else detail, origin_channel=origin_channel)
        # 2. 失败结束独立提交，排队输入继续等待后续明确操作。
        await self.save(state, checkpoint_type=CheckpointType.COMPLETED)

    #=====================应用启动退出时整理遗留执行======================

    async def unfinished_sessions(self) -> list[str]:
        """应用启动或退出整理时，找出数据库中仍有未结束执行记录的会话。

        Scheduler 查询后逐个调用 finalize_abandoned_turn，给旧轮保存结束结果。

        Returns:
            list[str]: 最新快照为 intermediate，或仍有等待信息、未完成工具调用的会话编号。
        """
        return [session for session, state, kind in await self._state_dao.latest_checkpoints()
                if kind == CheckpointType.INTERMEDIATE or state.get("interrupt_data") or state.get("pending_tool_calls")]

    async def finalize_abandoned_turn(
        self, *, partial: str = "", partial_persisted: bool = False,
        origin_channel: str = DESKTOP_CHANNEL, partial_message_id: str | None = None,
    ) -> bool:
        """应用启动或退出整理时，为已经失去执行者的旧轮次保存结束结果。

        Scheduler 在执行停止后调用。重新读取数据库中的进度，为遗留工具调用
        记录已保存的桌面回报、取消或结果未知；将退出前捕获的回答片段保存到
        对话中，再将旧轮标记为结束。整个处理过程使用已有数据完成。

        Args:
            partial (str): 退出前捕获的当前模型步骤已显示正文，启动时没有片段可传入。
            partial_persisted (bool): 片段在退出前是否已加入助手消息；本方法仍检查数据库中的消息。
            origin_channel (str): 捕获的回答片段所属渠道。
            partial_message_id (str | None): 当前助手消息编号，优先按编号查找需要补充结束说明的消息。

        Returns:
            bool: 已保存旧轮结束结果时为 True；会话已有结束快照且没有遗留调用时为 False。

        Raises:
            Exception: 保存失败时由调用方报告错误，数据库中的旧快照和对话历史保持原样。
        """
        # 1. 读取最新快照；已有完整结束记录且没有遗留工具或等待信息时，整理完成。
        state = await self.load()
        if self._state_dao.last_loaded_checkpoint_type != CheckpointType.INTERMEDIATE and not (
            state.interrupt_data or state.pending_tool_calls
        ):
            return False
        # 2. 按既有中断类型结束遗留调用，通用工具结果保持原有保存流程。
        interrupt = state.interrupt_data or {}
        content = APPLICATION_EXIT_RESULT
        if interrupt.get("type") == InterruptKind.PARENT_MESSAGE:
            content = PARENT_MESSAGE_DELIVERED
        state.close_unfinished_tools(content, APPLICATION_EXIT_RESULT, APPLICATION_EXIT_UNKNOWN_RESULT)
        # 3. 保存退出前已显示的回答；数据库中已有同一步骤时修改原消息，否则新增消息。
        rewritten_message_id = None
        if partial.strip():
            suffix = "……（应用退出，本轮已终止）"
            previous = None
            if partial_message_id is not None:
                # 3.1. 有消息编号时按编号查找，准确定位退出前正在生成的回答。
                previous = next((item for item in state.messages
                                 if isinstance(item, AssistantMessage)
                                 and item.message_id == partial_message_id), None)
            elif partial_persisted:
                # 3.2. 没有编号但调用方记录过该片段时，按正文和回答来源查找。
                previous = next((item for item in reversed(state.messages)
                                 if isinstance(item, AssistantMessage) and item.text == partial
                                 and item.origin_channel == origin_channel), None)
            if previous is not None:
                state.replace_message(with_text(previous, previous.text + suffix))
                rewritten_message_id = previous.message_id
            else:
                # 3.3. 数据库中尚无这一步骤时，新增捕获的正文及退出说明。
                state.add_assistant_message(partial + suffix, origin_channel=origin_channel)
        # 4. 清除本轮临时背景，统一保存工具结果、回答片段和已结束快照。
        state.clear_temporary_context()
        await self.save(state, checkpoint_type=CheckpointType.COMPLETED, rewritten_message_id=rewritten_message_id)
        return True

    async def reset_after_restart(self) -> None:
        """应用启动或关闭整理时，删除数据库中所有尚未处理的真人输入。

        Scheduler 调用此方法作废上次运行遗留的用户请求。已加入对话的输入记录、
        待处理任务结果和插件通知继续保留；之后的新用户消息由正常入口接收。
        """
        await self._inbox_dao.reset_after_restart()

    #=====================统一保存状态和图片======================

    async def save(self, state: AgentState, *, checkpoint_type: CheckpointType,
                   inbox_inputs: tuple[InboxEnvelope, ...] = (), rewritten_message_id: str | None = None,
                   release_queued: bool = False) -> int:
        """将会话状态保存为可恢复的快照，并同步保存对话历史和输入处理结果。

        输入保存、执行中保存、审批决定和轮次结束都通过这里写入。先准备消息和临时背景
        中的图片文件，再由 DAO 在同一数据库事务内保存快照、对话历史、Inbox
        已交付标记和按需执行的排队放行。数据库任一步失败时整笔事务回滚。

        Args:
            state (AgentState): 调用方正在使用、准备保存的会话状态。
            checkpoint_type (CheckpointType): intermediate 表示还会继续执行，completed 表示轮次已结束。
            inbox_inputs (tuple[InboxEnvelope, ...]): 已加入本次对话、需同时标记为已交付的 Inbox 记录。
            rewritten_message_id (str | None): 本次修改的已有助手消息编号，用于同步重写其历史记录。
            release_queued (bool): 是否在本次事务中将排队真人输入设为允许处理。

        Returns:
            int: 数据库提交成功后的 checkpoint 编号。

        Raises:
            Exception: 输入已失效或数据库提交失败，错误返回给调用方；已写出的图片文件不随数据库回滚。
        """
        # 1. 在线程中保存消息和临时背景图片，将内容替换为可持久化的文件引用。
        await asyncio.to_thread(self._externalize_images, state)
        # 2. 由同一个数据库事务保存会话进度、消息历史和输入状态，成功后返回快照编号。
        try:
            return await self._state_dao.save(state, checkpoint_type=checkpoint_type,
                inbox_inputs=inbox_inputs, rewritten_message_id=rewritten_message_id, release_queued=release_queued)
        except (InputNoLongerPending, ValueError):
            raise
        except Exception as exc:
            raise RequiredSaveError(str(exc)) from exc

    @classmethod
    def _externalize_images(cls, state: AgentState) -> None:
        """保存会话前将附件、工具结果和临时背景的图片转换为文件引用。

        Args:
            state (AgentState): 文件落盘成功后替换对应内容，保持消息身份和顺序。

        Raises:
            RequiredSaveError: 图片写入失败，本次数据库保存终止。
        """
        def save_content(content: tuple[ContentPart, ...]) -> tuple[ContentPart, ...]:
            """按图片来源保存同一消息或背景条目的图片。

            Args:
                content (tuple[ContentPart, ...]): 按显示顺序排列的图文。
            Returns:
                tuple[ContentPart, ...]: 清除内联编码、保留文件引用的图文。
            """
            groups: dict[MediaKind, list[tuple[int, ImageContent]]] = {}
            parts = list(content)
            for index, part in enumerate(content):
                if isinstance(part, ImageContent) and not part.storage_ref:
                    groups.setdefault(part.media_kind, []).append((index, part))
                elif isinstance(part, ImageContent) and part.url is not None:
                    parts[index] = replace(part, url=None)
            for kind, group in groups.items():
                try:
                    urls = [part.url for _, part in group if part.url is not None]
                    filenames = (save_multiple_images(urls) if kind is MediaKind.HUMAN
                                 else save_multiple_images(urls, category=kind))
                except Exception as exc:
                    raise RequiredSaveError("图片文件保存失败") from exc
                if len(filenames) != len(group):
                    raise RequiredSaveError("图片文件保存失败")
                for (index, part), filename in zip(group, filenames, strict=True):
                    parts[index] = replace(part, url=None, storage_ref=filename)
            return tuple(parts)

        # 1. 消息和临时背景共用落盘规则，数据库事务随后保存引用。
        for message in state.messages:
            if isinstance(message, (HumanMessage, AssistantMessage, ToolMessage)):
                content = save_content(message.content)
                if content != message.content:
                    state.replace_message(replace(message, content=content))
        for key, item in tuple(state.temporary_context.items()):
            images = save_content(item.images)
            state.set_temporary_context(key, item.model_copy(update={"images": images}))

    #=====================清除执行快照和释放资源======================

    async def pending_sessions(self) -> tuple[str, ...]:
        """启动整理时提供待交付输入所属会话，让 Scheduler 核对父配置。"""
        return await self._inbox_dao.pending_sessions()

    async def discard_pending_inputs(self) -> None:
        """父配置删除后清除待交付输入，已保存执行和已交付输入继续留档。"""
        await self._inbox_dao.discard_pending()

    async def discard_pending_commands(self) -> None:
        """丢弃旧子任务指令和回复，后台通知留给下次明确唤醒。"""
        await self._inbox_dao.discard_pending_commands()

    async def clear_session(self) -> int:
        """删除当前会话的执行快照，让后续 load 从新的空状态开始。

        完整对话历史和 Inbox 记录保留在数据库中；本方法的删除范围是 checkpoints 表。

        Returns:
            int: 本次删除的 checkpoint 数量。
        """
        return await self._state_dao.clear_session()

    async def close(self) -> None:
        """会话 worker、独立 Agent 或单次命令使用完本对象后，关闭两个数据库连接。

        先关闭会话快照存储，再关闭 Inbox 存储；前一个关闭报错时仍会尝试关闭后一个。
        """
        try:
            try:
                await self._state_dao.close()
            except Exception:
                logger.exception("关闭会话快照连接失败：%s", self.session_id)
        finally:
            try:
                await self._inbox_dao.close()
            except Exception:
                logger.exception("关闭会话输入连接失败：%s", self.session_id)
