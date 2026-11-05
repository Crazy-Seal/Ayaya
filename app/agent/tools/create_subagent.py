"""登记独立子身份与初始任务，保存成功后返回受理回执。"""

import asyncio
from collections.abc import Mapping

from app.agent.runtime.subagent_requests import CreateSubagentRequest
from app.agent.tools.core.base import BaseTool, ToolContext, ToolResult
from app.agent.tools._subagent_support import _runtime, _source, _receipt_result
from app.schemas.tool_identity import ToolIdentity


class CreateSubagentTool(BaseTool):
    """登记独立子身份与初始任务，保存成功后返回受理回执。

    Attributes:
        _visible_tool_identities (Mapping[str, ToolIdentity]): 工具管理器维护的只读公开名称与身份目录。
    """
    name = "create_subagent"
    description = "创建独立子 Agent，子Agent执行完任务后，任务结果会自动返回给你"
    parameters_schema = {"type": "object", "properties": {
        "name": {"type": "string", "description": "子 Agent 名称"},
        "system_prompt": {"type": "string", "description": "子 Agent 系统提示词，包含职责与行为要求等，如'你是一个xx助手/xx专家，你能够......'"},
        "prompt": {"type": "string", "description": "任务说明及完成任务所需其他信息"},
        "tool_whitelist": {"type": "array", "items": {"type": "string"}, "default": [],
                           "description": "选择子Agent能使用的工具白名单，名称填写你看到的完整工具名称"},
        "if_retain_context": {"type": "boolean", "default": False,
                            "description": "子Agent是否能够重复使用：取值为False时，子Agent任务完成时立即自动销毁；取值为True时，任务完成后可以再次给此Agent发送指令"},
    }, "required": ["name", "system_prompt", "prompt"], "additionalProperties": False}

    def __init__(self, visible_tool_identities: Mapping[str, ToolIdentity]) -> None:
        """保存创建工具选择白名单时使用的只读身份目录。

        Args:
            visible_tool_identities (Mapping[str, ToolIdentity]): 随模型目录和工具查找更新的名称与身份映射。
        """
        self._visible_tool_identities = visible_tool_identities

    async def execute(self, args: dict, context: ToolContext) -> ToolResult:
        """校验创建参数并解析工具白名单，登记初始任务后立即返回回执。

        Args:
            args (dict): 子定义、初始任务及公开工具名称列表。
            context (ToolContext): Pipeline 注入的调用来源和后台执行端口。

        Returns:
            ToolResult: 子身份、受理动作及受理时状态。

        Raises:
            ValueError: 名称、系统提示词或初始任务为空，或白名单包含当前不可见的公开名称。
        """
        # 1. 校验创建参数，在投递请求前拒绝空白名称、系统提示词或初始任务。
        if not args["name"].strip() or not args["system_prompt"].strip() or not args["prompt"].strip():
            raise ValueError("子名称、系统提示词及初始任务不能为空")
        runtime = _runtime(context)
        # 2. 从本次调用的可见目录查找身份；名称无效时不登记子 Agent。
        try:
            whitelist = frozenset(self._visible_tool_identities[name] for name in args.get("tool_whitelist", []))
        except KeyError as exc:
            raise ValueError(f"白名单工具不在当前可见目录中，请填写完整公开名称：{exc.args[0]}") from exc
        # 3. 队列及持久化继续使用稳定身份，公开名称只用于模型填写参数。
        reply = asyncio.get_running_loop().create_future()
        request = CreateSubagentRequest(_source(runtime), args["name"], args["system_prompt"], args["prompt"],
                                        whitelist,
                                        bool(args.get("if_retain_context", False)), reply)
        await runtime.runtime_queue.put(request)
        return _receipt_result(await reply)
