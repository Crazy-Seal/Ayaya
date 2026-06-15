"""read_file 工具 - 读取工作区内文件内容"""

from app.agent_v2.context import BaseTool, ToolContext, ToolResult
from app.agent_v2.utils.infra.log import log_tool_call_result
from app.agent_v2.utils.infra.safe_path import safe_path


class ReadFileTool(BaseTool):
    name = "read_file"
    description = (
        "读取指定路径的文件内容，并返回字符串结果。最多返回50000字符。\n"
        "Args:\n"
        "    path: 被读取的文件路径字符串。\n"
        "    limit: 可选参数，最多返回的行数。"
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "被读取的文件路径字符串。"},
            "limit": {"type": "integer", "description": "可选参数，最多返回的行数。"},
        },
        "required": ["path"],
    }

    async def execute(self, args: dict, context: ToolContext) -> ToolResult:
        path = args.get("path")
        limit = args.get("limit")
        result = self._read(path, limit)
        await log_tool_call_result(self.name, args, result)
        if isinstance(result, str) and result.startswith("错误:"):
            return ToolResult(content=result)
        return ToolResult.success(result)

    def _read(self, path: str, limit: int | None) -> str:
        try:
            # 统一按 UTF-8（兼容 BOM）读取，避免 Windows 默认编码导致乱码。
            text = safe_path(path).read_text(encoding="utf-8")
            lines = text.splitlines()
            if limit and limit < len(lines):
                lines = lines[:limit] + [f"... (剩余{len(lines) - limit}行被省略)"]
            return "\n".join(lines)[:50000]
        except UnicodeDecodeError:
            return "错误: 文件不是UTF-8编码，请先转换为UTF-8后再读取。"
        except Exception as e:
            return f"错误: {e}"
