"""将插件本地工具以独立身份接入现有执行管道。"""

from app.agent.tools.core.base import BaseTool, ToolContext, ToolResult
from app.agent.tools.core.naming import exposed_tool_name
from app.agent.tools.authorization.models import ToolIdentity
from app.schemas.tool_types import PLUGIN_TOOL_NAMESPACE, ToolSource


class PluginToolAdapter(BaseTool):
    """保留工具执行行为，同时绑定插件归属及模型公开名称。"""

    def __init__(self, plugin_name: str, tool: BaseTool) -> None:
        """保存当前 Agent 创建的工具实例及执行属性。

        Args:
            plugin_name (str): 提供该工具的插件稳定名称。
            tool (BaseTool): 插件声明所创建的本地工具。
        """
        self._tool = tool
        self.identity = ToolIdentity.local(tool.name, plugin_name=plugin_name)
        self.name = exposed_tool_name(PLUGIN_TOOL_NAMESPACE, plugin_name, ToolSource.LOCAL, tool.name)
        self.display_name = getattr(tool, "display_name", tool.name)
        self.description = tool.description
        self.parameters_schema = tool.parameters_schema
        self.compressible = tool.compressible
        self.persist_oversized_result = tool.persist_oversized_result
        self.authorization_timing = tool.authorization_timing

    async def execute(self, args: dict, context: ToolContext) -> ToolResult:
        """沿用插件工具的结果及恢复状态处理。

        Args:
            args (dict): 已校验的调用参数。
            context (ToolContext): 执行管道生成的原始上下文。

        Returns:
            ToolResult: 工具的原始执行结果。
        """
        return await self._tool.execute(args, context)
