"""按 ID 查询定义、最近父子交互和最终结果。"""

import asyncio
import json

from app.agent.runtime.subagent_requests import SubagentDetailRequest
from app.agent.tools.core.base import BaseTool, ToolContext, ToolResult
from app.agent.tools._subagent_support import _runtime, _source


class GetSubagentDetailsTool(BaseTool):
    """按 ID 查询定义、最近父子交互和最终结果。"""
    name = "get_subagent_details"
    parent_only = True
    description = "查询单个子 Agent 的提示词、工具范围、状态、最近父子交互及最终报告。全部当前名单已在环境消息中提供。"
    parameters_schema = {"type": "object", "properties": {
        "subagent_id": {"type": "string"}, "interaction_limit": {"type": "integer", "minimum": 1, "default": 10},
    }, "required": ["subagent_id"], "additionalProperties": False}

    async def execute(self, args: dict, context: ToolContext) -> ToolResult:
        """查询指定子 Agent 已保存的定义、交互和执行结果。

        Args:
            args (dict): 子 Agent 编号及需要返回的交互数量。
            context (ToolContext): 框架绑定的调用来源和共享请求队列。

        Returns:
            ToolResult: 已保存详情的 JSON 正文。
        """
        runtime = _runtime(context)
        reply = asyncio.get_running_loop().create_future()
        await runtime.runtime_queue.put(SubagentDetailRequest(_source(runtime), args["subagent_id"],
                                                              int(args.get("interaction_limit", 10)), reply))
        return ToolResult.success(json.dumps(await reply, ensure_ascii=False))
