"""edit_file 工具 - 替换工作区内文件的首次出现文本"""

from app.agent_v2.context import BaseTool, ToolContext, ToolResult
from app.agent_v2.utils.infra.log import log_tool_call_result
from app.agent_v2.utils.infra.safe_path import safe_path


class EditFileTool(BaseTool):
    name = "edit_file"
    description = (
        "将第一次出现处的旧文本替换为新文本\n"
        "Args:\n"
        "    path: 被修改的文件路径字符串。\n"
        "    old_text: 被替换的旧文本字符串。\n"
        "    new_text: 替换后的新文本字符串。"
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "被修改的文件路径字符串。"},
            "old_text": {"type": "string", "description": "被替换的旧文本字符串。"},
            "new_text": {"type": "string", "description": "替换后的新文本字符串。"},
        },
        "required": ["path", "old_text", "new_text"],
    }

    async def execute(self, args: dict, context: ToolContext) -> ToolResult:
        path = args.get("path")
        old_text = args.get("old_text", "")
        new_text = args.get("new_text", "")
        result = self._edit(path, old_text, new_text)
        await log_tool_call_result(self.name, args, result)
        if isinstance(result, str) and result.startswith("错误:"):
            return ToolResult(content=result)
        return ToolResult.success(result)

    def _edit(self, path: str, old_text: str, new_text: str) -> str:
        try:
            fp = safe_path(path)
            content = fp.read_text(encoding="utf-8")
            if old_text not in content:
                return f"错误: {path}中不存在目标文本'{old_text}'。"
            fp.write_text(content.replace(old_text, new_text, 1), encoding="utf-8")
            return f"已修改{path}"
        except Exception as e:
            return f"错误: {e}"
