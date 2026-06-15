"""MCP 工具适配器 - 把远端 MCP 工具包装成本框架的 BaseTool。"""

import logging
from typing import Any

from app.agent.context import BaseTool, ToolContext, ToolResult

logger = logging.getLogger(__name__)


class MCPToolAdapter(BaseTool):
    """把一个 MCP server 暴露的工具适配为 BaseTool，注册进 tool_manager。"""

    def __init__(self, mcp_tool: Any, session: Any):
        # 实例属性覆盖类注解
        self.name = mcp_tool.name
        self.description = getattr(mcp_tool, "description", "") or ""
        self.parameters_schema = getattr(mcp_tool, "inputSchema", None) or {
            "type": "object", "properties": {}
        }
        self._session = session

    async def execute(self, args: dict, context: ToolContext) -> ToolResult:
        try:
            result = await self._session.call_tool(self.name, args)
        except Exception as e:
            logger.error("MCP 工具 '%s' 调用失败: %s", self.name, e)
            return ToolResult.error(str(e))
        return ToolResult.success(self._extract_text(result))

    @staticmethod
    def _extract_text(result: Any) -> str:
        parts = getattr(result, "content", None) or []
        texts = []
        for part in parts:
            text = getattr(part, "text", None)
            if text:
                texts.append(text)
        return "\n".join(texts) if texts else "(无内容)"
