"""write_file 工具 - 在工作区内写入文件"""

from app.agent_v2.context import BaseTool, ToolContext, ToolResult
from app.agent_v2.utils.log import log_tool_call_result
from app.agent_v2.utils.safe_path import safe_path


class WriteFileTool(BaseTool):
    name = "write_file"
    description = (
        "在指定路径写入内容，如果文件不存在则创建。会覆盖原有内容。\n"
        "Args:\n"
        "    path: 被写入的文件路径字符串。\n"
        "    content: 要写入文件的内容字符串。"
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "被写入的文件路径字符串。"},
            "content": {"type": "string", "description": "要写入文件的内容字符串。"},
        },
        "required": ["path", "content"],
    }

    async def execute(self, args: dict, context: ToolContext) -> ToolResult:
        path = args.get("path")
        content = args.get("content", "")
        result = self._write(path, content)
        await log_tool_call_result(self.name, args, result)
        if isinstance(result, str) and result.startswith("错误:"):
            return ToolResult(content=result)
        return ToolResult.success(result)

    def _write(self, path: str, content: str) -> str:
        try:
            fp = safe_path(path)
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text(content, encoding="utf-8")
            return f"在{path}中写入了{len(content)}字节的内容。"
        except Exception as e:
            return f"错误: {e}"
