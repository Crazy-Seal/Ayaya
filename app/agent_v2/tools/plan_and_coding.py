"""plan_and_coding 工具 - 把编程任务派给编码子 Agent（后台执行，立即返回）。"""

import logging

from app.agent_v2.context import BaseTool, ToolContext, ToolResult
from app.agent_v2.subagents.coding import run_coding_subagent
from app.agent_v2.utils.background_tasks import create_background_task
from app.agent_v2.utils.log import log_tool_call_result

logger = logging.getLogger(__name__)


def _parse_todo_items(raw_items: str) -> list[dict]:
    """把多行原始待办文本解析为 todo 结构（id 从 1 递增，初始 pending）。"""
    items: list[dict] = []
    for line in raw_items.splitlines():
        text = line.strip()
        if not text:
            continue
        items.append({"id": str(len(items) + 1), "text": text, "status": "pending"})
    return items


async def _run(command: str, todo_items: list[dict], session_id: str) -> None:
    result = await run_coding_subagent(command, todo_items, session_id)
    logger.info("编码子 Agent 执行完成: session_id=%s, output=%s", session_id, result)


class PlanAndCodingTool(BaseTool):
    name = "plan_and_coding"
    description = (
        "创建编程任务的待办事项列表，并命令编程专家agent进行编程。需要进行编程时使用该工具。"
        "请将任务的不同步骤精确地拆解成多个待办事项，任务代码量大时，可将其按不同文件模块拆分成多个待办事项。"
        "如任务是新建项目，必须要有检查/安装环境依赖、创建xxx.py文件并写入代码、运行测试等步骤；"
        "如任务是修复bug，必须要有定位问题、修改代码、验证测试等步骤，确保编程专家能按待办事项逐步完成任务。"
        "command中必须完整清晰地描述要完成的任务或要修复的bug。"
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "raw_items": {
                "type": "string",
                "description": "编程任务的待办事项列表，每行一项，最多20项。",
            },
            "command": {
                "type": "string",
                "description": "对编程专家下的命令，如：用python画一个爱心",
            },
        },
        "required": ["raw_items", "command"],
    }

    async def execute(self, args: dict, context: ToolContext) -> ToolResult:
        raw_items = args.get("raw_items", "")
        command = args.get("command", "")
        result = await self._submit(raw_items, command, context.session_id)
        await log_tool_call_result(self.name, {"command": command}, result)
        if result.startswith("错误") or result.startswith("编程专家出错"):
            return ToolResult(content=result)
        return ToolResult.success(result)

    async def _submit(self, raw_items: str, command: str, session_id: str) -> str:
        try:
            todo_items = _parse_todo_items(raw_items)
        except Exception as e:
            return f"错误: {e}"
        if not session_id:
            return "编程专家出错: 缺少会话id信息"
        try:
            create_background_task(
                _run(command, todo_items, session_id),
                logger=logger,
                task_name=f"plan_and_coding:{session_id}",
            )
            return "任务已提交，正在后台执行。"
        except Exception as e:
            return f"编程专家出错: {e}"
