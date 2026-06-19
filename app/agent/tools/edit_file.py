"""edit_file 工具 - 替换工作区内文件的首次出现文本"""

from typing import Annotated

from app.agent.tools.decorator import tool
from app.agent.utils.infra.safe_path import safe_path


@tool
async def edit_file(
    path: Annotated[str, "被修改的文件路径字符串。"],
    old_text: Annotated[str, "被替换的旧文本字符串。"],
    new_text: Annotated[str, "替换后的新文本字符串。"],
) -> str:
    """将第一次出现处的旧文本替换为新文本。"""
    try:
        fp = safe_path(path)
        content = fp.read_text(encoding="utf-8")
        if old_text not in content:
            return f"错误: {path}中不存在目标文本'{old_text}'。"
        fp.write_text(content.replace(old_text, new_text, 1), encoding="utf-8")
        return f"已修改{path}"
    except Exception as e:
        return f"错误: {e}"
