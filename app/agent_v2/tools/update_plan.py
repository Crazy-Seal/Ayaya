"""update_plan 工具 - 更新编程任务的编码计划列表"""

from app.agent_v2.context import BaseTool, ToolContext, ToolResult
from app.agent_v2.utils.log import log_tool_call_result
from app.agent_v2.utils.todo_manager import TodoManager


class UpdatePlanTool(BaseTool):
    name = "update_plan"
    description = (
        "更新编程任务的编码计划列表。更新时，禁止修改id和text字段，仅更改status字段。\n"
        "只能有一条编码计划处于in_progress状态，其他必须是pending或completed状态。\n"
        "Args:\n"
        "    items: 列表，最多20项，每项为一个字典，包含\"id\"（序号），\"text\"（任务描述）"
        "和\"status\"（pending / in_progress / completed）字段。"
    )
    parameters_schema = {
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

    async def execute(self, args: dict, context: ToolContext) -> ToolResult:
        items = args.get("items", [])
        try:
            # 用 TodoManager 统一做合法性校验与渲染。
            todo_manager = TodoManager()
            todo_view = todo_manager.update(items)
            # 回写到状态扩展字段，供后续节点与 checkpoint 使用。
            # v2 中 todo_items 存放在 state.extra["todo_items"]，而非类型化字段。
            result = "编码计划列表已更新\n" + todo_view
        except Exception as e:
            result = f"错误: {e}"

        await log_tool_call_result(self.name, args, result)
        if isinstance(result, str) and result.startswith("错误:"):
            return ToolResult(content=result)

        return ToolResult.success(
            result,
            state_updates={"todo_items": list(todo_manager.items)},
        )
