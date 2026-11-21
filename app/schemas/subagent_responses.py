"""子 Agent 查询接口的名单和详情响应。"""

from pydantic import BaseModel

from app.schemas.subagent_types import SubagentInteractionKind, SubagentPublicStatus, SubagentSummary
from app.schemas.task_types import TaskRecord


class SubagentListResponse(BaseModel):
    """向界面交付父会话下的子 Agent 名单。

    Attributes:
        subagents (list[SubagentSummary]): 当前可查询的子 Agent 摘要。
    """

    subagents: list[SubagentSummary]


class SubagentInteractionResponse(BaseModel):
    """交付已保存的父子交互及原时间字符串。

    Attributes:
        interaction_id (int): 交互记录编号。
        subagent_id (str): 参与交互的子身份。
        kind (SubagentInteractionKind): 本条交互的业务类别。
        text (str): 交互正文。
        created_at (str): 已保存交互的创建时间。
        source_id (str): 交互来源编号。
    """

    interaction_id: int
    subagent_id: str
    kind: SubagentInteractionKind
    text: str
    created_at: str
    source_id: str


class SubagentDetailResponse(BaseModel):
    """向详情页交付子身份、可用状态、最近交互及历史结果。

    Attributes:
        subagent_id (str): 子身份编号。
        name (str): 子 Agent 名称。
        system_prompt (str): 创建时指定的系统提示词。
        if_retain_context (bool): 委派结束后是否保留上下文。
        created_at (str): 子身份创建时间。
        tool_whitelist (list[str]): 子 Agent 获准使用的工具稳定标识。
        context_available (bool): 子上下文是否仍可接收委派。
        recent_interactions (list[SubagentInteractionResponse]): 最近的父子显式交互。
        results (list[TaskRecord]): 已保存的委派执行结果。
        status (SubagentPublicStatus | None): 当前公开状态，上下文不可用时为空。
    """

    subagent_id: str
    name: str
    system_prompt: str
    if_retain_context: bool
    created_at: str
    tool_whitelist: list[str]
    context_available: bool
    recent_interactions: list[SubagentInteractionResponse]
    results: list[TaskRecord]
    status: SubagentPublicStatus | None
