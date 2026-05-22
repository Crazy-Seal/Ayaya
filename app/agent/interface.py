from abc import ABC, abstractmethod
from typing import AsyncIterator

from app.agent.events import StreamEvent
from app.schemas.chat import AgentInput


class BaseAgent(ABC):
    @abstractmethod
    async def ainvoke_agent_stream(self, user_message: AgentInput) -> AsyncIterator[StreamEvent]:
        """流式调用入口，子类实现具体逻辑。"""

    @abstractmethod
    def rollback_thread_checkpoints(self, checkpoint_ns: str = "") -> tuple[int, int]:
        """回滚本轮会话中基线之后写入的 checkpoint。"""
