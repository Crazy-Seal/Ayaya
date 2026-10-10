"""一轮运行的停止和引导信号。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import AsyncIterator, TypeVar

from app.schemas.runtime_types import TurnInterruptReason, TurnPhase


T = TypeVar("T")


class TurnInterrupted(Exception):
    """通知 Agent 在模型或工具安全点结束当前轮并持久化收尾。

    """


@dataclass
class TurnControl:
    """管理一个 run 的打断信号和可见片段，只取消模型读取任务而不取消会话 worker。

    Attributes:
        run_id (str): 当前运行轮次标识。
        reason (TurnInterruptReason | None): 已受理的首个停止或引导信号，未打断时为空。
        phase (TurnPhase): 当前轮次内部执行阶段，提交后拒绝打断，不与会话阶段混用。
        partial (str): 本次模型步骤已向用户展示的文本。
        partial_persisted (bool): 该片段是否已记录到 Assistant 步骤。
        partial_message_id (str | None): 当前模型步骤的 Assistant 身份，落盘状态需核对 checkpoint。
        _llm_task (asyncio.Task | None): 可单独取消的模型下一分片等待任务。
    """
    run_id: str
    reason: TurnInterruptReason | None = None
    phase: TurnPhase = TurnPhase.IDLE
    partial: str = ""
    partial_persisted: bool = False
    partial_message_id: str | None = None
    _llm_task: asyncio.Task | None = field(default=None, repr=False)

    def request(self, reason: TurnInterruptReason) -> bool:
        """记录首个停止或引导原因，进入最终提交后拒绝新的打断。

        Args:
            reason (TurnInterruptReason): 停止或引导原因，既有字符串调用按相同值还原。

        Returns:
            bool: 本次操作是否满足条件并被受理。
        """
        # 1. 最终提交不可再打断，避免已提交事实与界面停止状态分离。
        if self.phase == TurnPhase.COMMITTING:
            return False
        # 2. 保留首个原因，重复请求只确认已经受理。
        if self.reason is not None:
            return True
        self.reason = TurnInterruptReason(reason)
        # 3. 只取消下一分片等待；工具与持久收尾由原 worker 完成。
        if self.phase == TurnPhase.LLM and self._llm_task is not None:
            self._llm_task.cancel()
        return True

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
                T: 当前查询或执行结果。
            """
            return await anext(stream)

        task = asyncio.create_task(read_next())
        self._llm_task = task
        if self.reason is not None:
            task.cancel()
        try:
            return await task
        # 2. 用户停止转成 TurnInterrupted；worker 自身被应用关闭取消时继续传播取消。
        except asyncio.CancelledError:
            owner = asyncio.current_task()
            if self.reason is not None and (owner is None or not owner.cancelling()):
                # 2.1. 旧调用可能以原字符串初始化，异常正文必须仍是 stop 或 steer。
                reason = self.reason.value if isinstance(self.reason, TurnInterruptReason) else self.reason
                raise TurnInterrupted(reason) from None
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
        if self.reason is not None:
            # 1. 兼容既有字符串初始化；具名枚举不能以成员名称进入异常文案。
            reason = self.reason.value if isinstance(self.reason, TurnInterruptReason) else self.reason
            raise TurnInterrupted(reason)

    def start_llm(self) -> None:
        """检查打断信号并重置当前模型步骤的可见片段。

        """
        self.check()
        self.phase = TurnPhase.LLM
        self.partial = ""
        self.partial_persisted = False
        self.partial_message_id = None

    def start_tool(self) -> None:
        """在工具产生副作用前检查打断并进入工具阶段。

        """
        self.check()
        self.phase = TurnPhase.TOOL

    def begin_commit(self) -> None:
        """检查最后一个打断安全点并封住最终提交阶段。

        """
        self.check()
        self.phase = TurnPhase.COMMITTING

    def append(self, text: str) -> None:
        """累积已经向用户展示的模型文本，供停止后保存。

        Args:
            text (str): 需要保存或追加的文本。
        """
        self.partial += text
