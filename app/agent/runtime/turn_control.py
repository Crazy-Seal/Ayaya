"""一轮运行的停止和引导信号。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import AsyncIterator, TypeVar

from app.schemas.runtime_types import TurnInterruptReason, ExecutionPhase, ExecutionWait
from app.agent.runtime.agent_run_state_machine import AgentRunStateMachine


T = TypeVar("T")


class TurnInterrupted(Exception):
    """通知 Agent 在模型或工具安全点结束当前轮并持久化收尾。"""


@dataclass
class TurnControl:
    """管理一个 run 的停止、引导信号和可见片段，在安全点交还执行控制。

    Attributes:
        run_id (str): 当前运行轮次标识。
        reason (TurnInterruptReason | None): 状态机受理的停止或引导原因，停止可接管正在结束的引导。
        _state (AgentRunStateMachine): 执行对象借给本轮的唯一运行状态。
        phase (ExecutionPhase): 共享状态机的只读阶段，最终提交及收尾时拒绝打断。
        partial (str): 本次模型步骤已向用户展示的文本。
        partial_persisted (bool): 该片段是否已记录到 Assistant 步骤。
        partial_message_id (str | None): 当前模型步骤的 Assistant 身份，落盘状态需核对 checkpoint。
        _llm_task (asyncio.Task | None): 可单独取消的模型下一分片等待任务。
        _tool_task (asyncio.Task | None): 当前工具执行任务，用户停止时取消并等待其清理。
    """
    run_id: str
    _state: AgentRunStateMachine = field(repr=False)
    partial: str = ""
    partial_persisted: bool = False
    partial_message_id: str | None = None
    _llm_task: asyncio.Task | None = field(default=None, repr=False)
    _tool_task: asyncio.Task[object] | None = field(default=None, repr=False)

    @property
    def phase(self) -> ExecutionPhase:
        """读取执行所有者维护的阶段，供安全点诊断。"""
        return self._state.phase

    @property
    def reason(self) -> TurnInterruptReason | None:
        """读取当前轮次已受理的停止原因，旧控制对象保持失效。"""
        return self._state.reason if self._state.run_id == self.run_id else None

    def wake_interrupt(self) -> None:
        """受理引导时唤醒模型等待，受理停止时同时取消当前工具。"""
        if self._llm_task is not None:
            self._llm_task.cancel()
        if self.reason == TurnInterruptReason.STOP and self._tool_task is not None and not self._tool_task.cancelling():
            self._tool_task.cancel()

    async def run_tool(self, operation: Callable[[], Awaitable[T]]) -> T:
        """执行当前工具，用户停止时等待工具清理后交还本轮收尾。

        Args:
            operation (Callable[[], Awaitable[T]]): 本次实际工具调用，启动前检查通过后才创建协程。

        Returns:
            T: 工具正常返回的真实结果，后续钩子和保存由 Pipeline 组织。

        Raises:
            TurnInterrupted: 启动前收到控制请求，或工具被用户停止取消且已完成清理。
            asyncio.CancelledError: 外层执行被关闭，或工具自身取消，继续向调用方传播。
        """
        # 1. 启动前及任务实际开始时核对本轮，控制请求可阻止尚未开始的副作用。
        self.start_tool()

        async def invoke() -> T:
            """在本次工具任务开始时检查控制，再执行工具正文。

            Returns:
                T: 工具正文返回的结果，交给 Pipeline 后续处理。
            """
            self.check()
            return await operation()

        task = asyncio.create_task(invoke())
        self._tool_task = task
        try:
            return await task
        except asyncio.CancelledError:
            # 2. 用户停止只取消工具任务；外层关闭保留原取消语义。
            owner = asyncio.current_task()
            if self.reason == TurnInterruptReason.STOP and (owner is None or not owner.cancelling()):
                raise TurnInterrupted(TurnInterruptReason.STOP.value) from None
            raise
        finally:
            # 3. 工具返回或完成清理后交还结果，完成钩子及保存按原顺序执行。
            if self._tool_task is task:
                self._tool_task = None

    @property
    def wait(self) -> ExecutionWait | None:
        """向 Agent 交还本轮已保存的互斥等待材料。"""
        return self._state.wait

    def begin_interrupted_commit(self) -> None:
        """旧模型工具已结束，开始保护停止或引导所需的协议保存。"""
        self._state.begin_commit(self.run_id)

    def begin_failed_commit(self) -> None:
        """执行业务失败时结束当前等待，并保护失败结果的最终保存。"""
        if self._state.phase == ExecutionPhase.WAITING:
            self._state.request_stop(self.run_id)
        if self._state.phase != ExecutionPhase.COMMITTING:
            self._state.begin_commit(self.run_id)

    def wait_saved(self, kind: str, request_id: str) -> None:
        """Pipeline 确认等待事务后登记请求，迟到保存保留已接受的停止。

        Args:
            kind (str): 已保存的审批或父消息等待类别。
            request_id (str): 本次等待的持久编号。
        """
        from app.schemas.runtime_types import ApprovalWait, ParentMessageWait, InterruptKind
        wait = ApprovalWait(request_id) if kind == InterruptKind.AUTHORIZATION_REQUEST else ParentMessageWait(request_id)
        self._state.wait_saved(self.run_id, wait)
        self.check()

    async def next_llm_chunk(self, stream: AsyncIterator[T]) -> T:
        """读取一个模型分片，将用户取消转换为安全点异常并保留外部任务取消。

        Args:
            stream (AsyncIterator[T]): 异步模型或事件迭代器。

        Returns:
            T: 模型迭代器提供的下一分片。

        Raises:
            TurnInterrupted: 在读取前或等待期间收到了用户停止、引导信号。
            StopAsyncIteration: 模型流已正常结束。
            asyncio.CancelledError: 会话 worker 本身正在关闭，继续向调用方传播取消。
        """
        # 1. 分片读取前检查信号，并将读取隔离到可单独取消的子任务。
        self.check()

        async def read_next() -> T:
            """在独立可取消任务中等待模型迭代器的下一项。

            Returns:
                T: 当前等待返回的下一条模型片段，停止或引导信号到达时会中断等待。
            """
            return await anext(stream)

        task = asyncio.create_task(read_next())
        self._llm_task = task
        try:
            return await task
        # 2. 用户停止转成 TurnInterrupted；worker 自身被应用关闭取消时继续传播取消。
        except asyncio.CancelledError:
            owner = asyncio.current_task()
            if self.reason is not None and (owner is None or not owner.cancelling()):
                raise TurnInterrupted(self.reason.value) from None
            raise
        # 3. 清除本次分片任务引用，避免后续请求取消已结束的等待。
        finally:
            if self._llm_task is task:
                self._llm_task = None

    def check(self) -> None:
        """在安全点检查已经受理的打断信号。

        Raises:
            TurnInterrupted: 已受理的停止或引导信号要求结束当前轮。
        """
        if self._state.run_id != self.run_id:
            raise TurnInterrupted("执行轮次已结束")
        if self.reason is not None:
            # 1. 使用原因的协议值通知 Agent，供其选择停止或引导收尾。
            raise TurnInterrupted(self.reason.value)

    def is_interrupted(self) -> bool:
        """向工具提供只读打断状态，线程不得取消 worker 或推进轮次阶段。

        Returns:
            bool: 停止或引导请求已经受理时为 True。
        """
        return self.reason is not None

    def start_llm(self) -> None:
        """检查打断信号并重置当前模型步骤的可见片段。"""
        self.check()
        self._state.check_running(self.run_id)
        self.partial = ""
        self.partial_persisted = False
        self.partial_message_id = None

    def start_tool(self) -> None:
        """在工具产生副作用前校验本轮仍处于 RUNNING。"""
        self.check()
        self._state.check_running(self.run_id)

    def begin_commit(self) -> None:
        """检查最后一个打断安全点并封住最终提交阶段。"""
        self.check()
        self._state.begin_commit(self.run_id)

    def append(self, text: str) -> None:
        """累积已经向用户展示的模型文本，供停止后保存。

        Args:
            text (str): 需要保存或追加的文本。
        """
        self.partial += text
