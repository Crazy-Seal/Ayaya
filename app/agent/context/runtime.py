"""单轮模型工具循环共享的易变上下文。"""

from dataclasses import dataclass
from datetime import datetime

from app.agent.state.time import utc_now


@dataclass
class RuntimeContext:
    """一轮模型工具循环共享、但绝不持久化的易变上下文。"""

    captured_at: datetime
    latest_human_message_id: str = ""
    recalled_memory: str = ""

    @classmethod
    def capture(cls) -> "RuntimeContext":
        return cls(captured_at=utc_now())
