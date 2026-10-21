"""单轮模型工具循环共享的易变上下文。"""

from dataclasses import dataclass
from datetime import datetime

from app.schemas.runtime_types import (
    DESKTOP_CHANNEL,
)
from app.agent.state.time import utc_now
from app.agent.runtime.turn_control import TurnControl


@dataclass
class RuntimeContext:
    """保存同一轮模型工具循环共享的临时来源和控制信号。

    Attributes:
        captured_at (datetime): 本轮开始时捕获的 UTC 时间。
        latest_human_message_id (str): 当前真人输入标识，内部通知轮次为空。
        recalled_memory (str): 本轮临时注入的检索记忆。
        origin_channel (str): 输入或本轮回复所属的渠道。
        turn_control (TurnControl | None): 当前 run 的停止与引导控制器。
    """

    captured_at: datetime
    latest_human_message_id: str = ""
    recalled_memory: str = ""
    origin_channel: str = DESKTOP_CHANNEL
    turn_control: TurnControl | None = None

    @classmethod
    def capture(cls) -> "RuntimeContext":
        """在新轮次开始时记录当前 UTC 时间，其他临时输入信息由调用方随后填写。

        Returns:
            RuntimeContext: 尚未绑定真人消息、来源和停止信号的本轮上下文。
        """
        return cls(captured_at=utc_now())
