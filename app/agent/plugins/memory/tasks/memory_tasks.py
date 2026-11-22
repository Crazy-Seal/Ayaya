"""独立静默记忆任务，材料由输入提交前准备，执行不依赖原插件实例。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.agent.runtime.tasks.base import BaseTask
from app.agent.runtime.tasks.runtime import TaskRuntime
from app.agent.state.messages import HumanMessage
from app.agent.plugins.memory.memories.diary import DiaryGeneration

if TYPE_CHECKING:
    from app.agent.plugins.memory.manager import MemoryManager
    from app.agent.plugins.memory.operation import MemoryOperation

logger = logging.getLogger(__name__)
MEMORY_HISTORY_HUMAN_MESSAGES = 5


@dataclass
class DiaryTask(BaseTask):
    """使用已确认的日期和对话生成日记，结束时释放该日期的处理中标记。

    Attributes:
        operation (MemoryOperation): 提交时的配置与连接工厂快照。
        generation (DiaryGeneration): 提交前准备好的日记材料及处理中标记。
    """

    operation: MemoryOperation
    generation: DiaryGeneration

    async def run(self, runtime: TaskRuntime) -> str:
        """生成并保存日记，成功返回摘要，失败由 Supervisor 记录错误。

        Args:
            runtime (TaskRuntime): 框架绑定插件、会话及任务编号的调用对象。

        Returns:
            str: 包含日记日期的成功摘要。
        """
        async with self.operation.open(self.generation.session_id, diary=True) as manager:
            return await manager.generate_diary(self.generation)

    async def cleanup(self) -> None:
        """生成结束或任务启动前被停止时释放标记，让后续激活能够重新检查。"""
        self.generation.release()


@dataclass
class LongTermMemoryTask(BaseTask):
    """按固定完整轮次提取长期记忆，保留已确认的记忆业务语义。

    Attributes:
        operation (MemoryOperation): 提交时的配置与连接工厂快照。
        session_id (str): 固定的来源会话编号。
        cutoff (int): 输入保存前确定的历史最大编号。
        batch_turns (int): 本批已经完成的真人轮次数。
        enable_episodic (bool): 是否提取情景记忆。
        enable_semantic (bool): 是否提取语义记忆。
    """

    operation: MemoryOperation
    session_id: str
    cutoff: int
    batch_turns: int
    enable_episodic: bool
    enable_semantic: bool

    async def run(self, runtime: TaskRuntime) -> str:
        """读取固定轮次并返回提取数量，跳过返回原因，失败交给 Supervisor 保存。

        Args:
            runtime (TaskRuntime): 框架绑定插件、会话及任务编号的调用对象。

        Returns:
            str: 两类记忆的完成数量，或跳过本次提取的具体原因。
        """
        async with self.operation.open(self.session_id, episodic=self.enable_episodic,
                                       semantic=self.enable_semantic) as manager:
            return await self._extract(manager)

    async def _extract(self, manager: MemoryManager) -> str:
        """从固定历史范围提取记忆并汇总面板结果。

        Args:
            manager (MemoryManager): 当前任务独占的资源和业务对象。

        Returns:
            str: 已保存数量或跳过原因。
        """
        store = manager.chat_history_store
        if store is None:
            logger.warning("协议日志查询器未启用，跳过长期记忆")
            return "跳过长期记忆提取：协议日志查询器未启用。"
        history, recent = await store.get_recent_completed_turns(self.session_id,
            current_turns=self.batch_turns, history_turns=MEMORY_HISTORY_HUMAN_MESSAGES,
            max_history_id=self.cutoff)
        if sum(isinstance(message, HumanMessage) for message in recent) < self.batch_turns:
            logger.warning("完整协议轮次不足，跳过本次长期记忆提取")
            return "跳过长期记忆提取：完整协议轮次不足。"
        counts = await manager.add(recent, history,
            enable_episodic=self.enable_episodic, enable_semantic=self.enable_semantic)
        episodic = (
            f"已保存 {counts['episodic']} 条"
            if self.enable_episodic and manager.episodic_memory is not None else "未启用"
        )
        semantic = (
            f"处理 {counts['semantic']} 条"
            if self.enable_semantic and manager.semantic_memory is not None else "未启用"
        )
        return f"长期记忆提取完成；情景记忆：{episodic}；语义记忆：{semantic}。"
