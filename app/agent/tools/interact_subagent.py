"""统一发送命令、停止当前执行或销毁子上下文。"""

import asyncio

from app.agent.runtime.subagent_requests import InteractSubagentRequest
from app.agent.tools.core.base import BaseTool, ToolContext, ToolResult
from app.agent.tools._subagent_support import _runtime, _source, _receipt_result
from app.schemas.subagent_types import SubagentAction


class InteractSubagentTool(BaseTool):
    """子 Agent 交互工具"""
    name = "interact_subagent"
    description = "与子 Agent 交互。command 给子 agent 发送消息；stop 停止子 agent 执行，destroy 销毁子 agent。"
    parameters_schema = {"type": "object", "properties": {
        "subagent_id": {"type": "string"}, "action": {"type": "string", "enum": [action.value for action in SubagentAction]},
        "text": {"type": "string"},
    }, "required": ["subagent_id", "action"], "additionalProperties": False}

    async def execute(self, args: dict, context: ToolContext) -> ToolResult:
        """根据目标当前状态受理命令、停止或销毁，保存成功后返回回执。

        Args:
            args (dict): 目标身份、操作和命令正文。
            context (ToolContext): 框架绑定的调用来源及请求队列。

        Returns:
            ToolResult: 目标身份、已受理动作及受理时状态。
        """
        runtime = _runtime(context)
        reply = asyncio.get_running_loop().create_future()
        await runtime.runtime_queue.put(InteractSubagentRequest(_source(runtime), args["subagent_id"],
                                         SubagentAction(args["action"]), args.get("text"), reply))
        return _receipt_result(await reply)
