"""独立静默记忆任务，材料由输入提交前准备，执行不依赖原插件实例。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.agent.runtime.tasks.base import BaseTask
from app.agent.plugins.runtime import PluginRuntime
from app.agent.state.messages import HumanMessage

if TYPE_CHECKING:
    from app.agent.memory.manager import MemoryManager

logger = logging.getLogger(__name__)
MEMORY_HISTORY_HUMAN_MESSAGES = 5


@dataclass
class DiaryTask(BaseTask):
    """按准备好的历史截止位置整理日记，Manager 由应用记忆缓存持有。

    Attributes:
        manager (MemoryManager): 应用缓存持有的记忆业务对象。
        cutoff (int): 输入保存前确定的历史最大编号。
    """

    manager: MemoryManager
    cutoff: int

    async def run(self, runtime: PluginRuntime) -> None:
        """等待日记整理完成；运行日志不作为任务结果返回。

        Args:
            runtime (PluginRuntime): 框架绑定插件、会话、来源轮次及任务编号的调用对象。
        """
        await self.manager.generate_diary(max_history_id=self.cutoff)


@dataclass
class LongTermMemoryTask(BaseTask):
    """按固定完整轮次提取长期记忆，保留已确认的记忆业务语义。

    Attributes:
        manager (MemoryManager): 应用缓存持有的记忆业务对象。
        session_id (str): 固定的来源会话编号。
        cutoff (int): 输入保存前确定的历史最大编号。
        batch_turns (int): 本批已经完成的真人轮次数。
        enable_episodic (bool): 是否提取情景记忆。
        enable_semantic (bool): 是否提取语义记忆。
    """

    manager: MemoryManager
    session_id: str
    cutoff: int
    batch_turns: int
    enable_episodic: bool
    enable_semantic: bool

    async def run(self, runtime: PluginRuntime) -> None:
        """读取固定历史范围并等待提取完成，缺少完整材料时沿用跳过行为。

        Args:
            runtime (PluginRuntime): 框架绑定插件、会话、来源轮次及任务编号的调用对象。
        """
        store = self.manager.chat_history_store
        if store is None:
            logger.warning("协议日志查询器未启用，跳过长期记忆")
            return
        history, recent = await store.get_recent_completed_turns(self.session_id,
            current_turns=self.batch_turns, history_turns=MEMORY_HISTORY_HUMAN_MESSAGES,
            max_history_id=self.cutoff)
        if sum(isinstance(message, HumanMessage) for message in recent) < self.batch_turns:
            logger.warning("完整协议轮次不足，跳过本次长期记忆提取")
            return
        await self.manager.add(recent, history,
            enable_episodic=self.enable_episodic, enable_semantic=self.enable_semantic)
