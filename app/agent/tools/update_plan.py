"""update_plan 工具 - 更新编程任务的编码计划列表"""

from app.agent.context import ToolContext, ToolResult
from app.agent.subagents.todo_manager import TodoManager
from app.agent.tools.decorator import tool

# 嵌套 array-of-object 参数，签名无法自动表达，用 @tool 的显式 schema 逃生口。
_UPDATE_PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "description": "最多20项的编码计划列表。",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "序号。"},
                    "text": {"type": "string", "description": "任务描述。"},
                    "status": {
                        "type": "string",
                        "enum": ["pending", "in_progress", "completed"],
                        "description": "任务状态。",
                    },
                },
                "required": ["id", "text", "status"],
            },
        },
    },
    "required": ["items"],
}


@tool(parameters_schema=_UPDATE_PLAN_SCHEMA)
async def update_plan(items: list, context: ToolContext) -> ToolResult:
    """更新编程任务的编码计划列表。更新时，禁止修改id和text字段，仅更改status字段。只能有一条编码计划处于in_progress状态，其他必须是pending或completed状态。items为列表，最多20项，每项为一个字典，包含"id"（序号）、"text"（任务描述）和"status"（pending / in_progress / completed）字段。"""
    try:
        # 用 TodoManager 统一做合法性校验与渲染。
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
