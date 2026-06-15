"""delete_file 工具 - 删除工作区内文件"""

from app.agent_v2.context import BaseTool, ToolContext, ToolResult
from app.agent_v2.utils.infra.log import log_tool_call_result
from app.agent_v2.utils.infra.safe_path import safe_path


class DeleteFileTool(BaseTool):
    name = "delete_file"
    description = (
        "删除指定路径的文件或文件夹。\n"
        "Args:\n"
        "    path: 被删除的文件/文件夹路径字符串。"
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "被删除的文件/文件夹路径字符串。"},
        },
        "required": ["path"],
    }

    async def execute(self, args: dict, context: ToolContext) -> ToolResult:
        path = args.get("path")
        result = self._delete(path)
        await log_tool_call_result(self.name, args, result)
        if isinstance(result, str) and result.startswith("错误:"):
            return ToolResult(content=result)
        return ToolResult.success(result)

    def _delete(self, path: str) -> str:
        try:
            fp = safe_path(path)
            if not fp.exists():
                return f"错误: {path}不存在。"

            fp.unlink()
            return f"已删除{path}"
        except Exception as e:
            return f"错误: {e}"
