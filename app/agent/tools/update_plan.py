"""update_plan 工具 - 更新计划列表"""

from typing import Annotated, Literal

from pydantic import BaseModel, Field

from app.agent.tools.base import ToolContext, ToolResult
from app.agent.tools.decorator import tool


class TodoManager:
    """校验本次工具提交的计划列表并生成显示文本，长期状态由工具结果交给执行管道保存。"""
    def __init__(self):
        """创建空计划列表，此时不读取或保存 AgentState。"""
        self.items = []

    def update(self, items: list) -> str:
        """先校验整个计划的条数、正文与状态，只允许一项进行中，全部成功后替换内存列表。

        Args:
            items (list): 按原顺序提供的条目。

        Returns:
            str: 新计划的状态标记和完成数量文本。

        Raises:
            ValueError: 超过二十项、正文为空、状态未知，或多项同时进行中。
        """
        if len(items) > 20:
            raise ValueError("最多只能有20个待办事项")
        # 1. 整个列表先在局部校验，任何项目无效都不会覆盖当前计划。
        validated = []
        in_progress_count = 0
        for i, item in enumerate(items):
            text = str(item.get("text", "")).strip()
            status = str(item.get("status", "pending")).lower()
            item_id = str(item.get("id", str(i + 1)))
            if not text:
                raise ValueError(f"Item {item_id}: text required")
            if status not in ("pending", "in_progress", "completed"):
                raise ValueError(f"Item {item_id}: invalid status '{status}'")
            if status == "in_progress":
                in_progress_count += 1
            validated.append({"id": item_id, "text": text, "status": status})
        if in_progress_count > 1:
            raise ValueError("总共只能有一个待办事项处于进行中")
        # 2. 检查只有一项进行中后才更新内存；AgentState 更新由工具返回的 state_updates 承载。
        self.items = validated
        return self.render()

    def render(self) -> str:
        """为当前计划生成待办、进行中、完成标记及完成数量。

        Returns:
            str: 格式化计划文本，无任务时返回列表为空的说明。
        """
        if not self.items:
            return "事项列表为空"
        lines = []
        for item in self.items:
            marker = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}[item["status"]]
            lines.append(f"{marker} #{item['id']}: {item['text']}")
        done = sum(1 for t in self.items if t["status"] == "completed")
        lines.append(f"\n({done}/{len(self.items)} 已完成)")
        return "\n".join(lines)


# 计划字段用于生成模型可见 Schema；类文档只说明任务数据，不加入内部实现细节。
class TodoItem(BaseModel):
    """计划中的一项任务，包含稳定编号、任务描述和待办、进行中或完成状态。"""
    id: str = Field(description="序号。")
    text: str = Field(description="任务描述。")
    status: Literal["pending", "in_progress", "completed"] = Field(description="任务状态。")


@tool
async def update_plan(
    items: Annotated[list[TodoItem], "最多20项的计划列表。"],
    context: ToolContext,
) -> ToolResult:
    """更新计划列表。更新时，禁止修改id和text字段，仅更改status字段。只能有一条计划处于in_progress状态，其他必须是pending或completed状态。items为列表，最多20项，每项为一个字典，包含"id"（序号）、"text"（任务描述）和"status"（pending / in_progress / completed）字段。

    Args:
        items (list[TodoItem]): 最多20项的计划列表。
        context (ToolContext): 执行管道提供的工具运行上下文。
    """
    # 注意：items 的类型注解 list[TodoItem] 仅用于生成 schema；
    # 这里收到的items仍是 list[dict]，TodoManager.update 直接拿 dict。
    try:
        todo_manager = TodoManager()
        todo_view = todo_manager.update(items or [])
        result = "计划列表已更新\n" + todo_view
    except Exception as e:
        return ToolResult(content=f"错误: {e}")

    # 回写到状态扩展字段（state.extra["todo_items"]），供后续节点与 checkpoint 使用。
    return ToolResult.success(
        result,
        state_updates={"todo_items": list(todo_manager.items)},
    )
