"""后台任务查询的 HTTP 响应，复用已有任务数据字段。"""

from dataclasses import dataclass

from pydantic import BaseModel

from app.schemas.task_types import TaskSnapshot


@dataclass(frozen=True)
class TaskListItem(TaskSnapshot):
    """为任务列表中的完整快照附加最近终端输出。

    Attributes:
        recent_output (str): 供任务列表预览的有限输出片段。
    """

    recent_output: str = ""


class TaskListResponse(BaseModel):
    """向任务面板交付当前会话的任务及输出预览。

    Attributes:
        tasks (list[TaskListItem]): 完整任务快照和最近输出。
    """

    tasks: list[TaskListItem]


class TaskOutputResponse(BaseModel):
    """按字节游标交付任务的一页终端输出。

    Attributes:
        text (str): 当前页的完整文本。
        next_offset (int): 下一次读取的字节位置。
        has_more (bool): 当前输出文件中是否还有后续内容。
    """

    text: str
    next_offset: int
    has_more: bool
