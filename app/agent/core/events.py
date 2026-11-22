"""定义 Agent 通过异步事件流交给会话调度器的执行事件。"""

from dataclasses import dataclass
from enum import Enum
from typing import Any


class EventType(str, Enum):
    """定义 Agent 报告的执行开始、输入提交、正文、工具开始与完成、中断和结束事件。"""

    RUN_STARTED = "run_started"
    HUMAN_INPUT_COMMITTED = "human_input_committed"
    TEXT_CHUNK = "text_chunk"
    TOOL_CALL = "tool_call"
    TOOL_COMPLETED = "tool_completed"
    INTERRUPT = "interrupt"
    ERROR = "error"
    DONE = "done"


@dataclass
class AgentEvent:
    """由 Agent 或 Pipeline 产生，交给 Scheduler 转换为界面可见事件。

    Attributes:
        type (EventType): 当前执行事件的类型。
        data (Any): 当前事件的文字、输入提交事实或中断等数据。
    """

    type: EventType
    data: Any
