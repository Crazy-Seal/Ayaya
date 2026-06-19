"""update_plan 工具 - 更新编程任务的编码计划列表"""

from typing import Annotated, Literal

from pydantic import BaseModel, Field

from app.agent.context import ToolContext, ToolResult
from app.agent.subagents.todo_manager import TodoManager
from app.agent.tools.decorator import tool


# 单条编码计划。仅用于让 @tool 自动生成嵌套 schema；不写 docstring，避免实现说明泄漏给 LLM。
class TodoItem(BaseModel):
    id: str = Field(description="序号。")
    text: str = Field(description="任务描述。")
    status: Literal["pending", "in_progress", "completed"] = Field(description="任务状态。")


@tool
async def update_plan(
    items: Annotated[list[TodoItem], "最多20项的编码计划列表。"],
    context: ToolContext,
) -> ToolResult:
    """更新编程任务的编码计划列表。更新时，禁止修改id和text字段，仅更改status字段。只能有一条编码计划处于in_progress状态，其他必须是pending或completed状态。items为列表，最多20项，每项为一个字典，包含"id"（序号）、"text"（任务描述）和"status"（pending / in_progress / completed）字段。"""
    # 注意：items 的类型注解 list[TodoItem] 仅用于生成 schema；
    # 这里收到的items仍是 list[dict]，TodoManager.update 直接拿 dict。
    try:
        todo_manager = TodoManager()
        todo_view = todo_manager.update(items or [])
        result = "编码计划列表已更新\n" + todo_view
    except Exception as e:
        return ToolResult(content=f"错误: {e}")

    # 回写到状态扩展字段（state.extra["todo_items"]），供后续节点与 checkpoint 使用。
    return ToolResult.success(
        result,
        state_updates={"todo_items": list(todo_manager.items)},
    )
