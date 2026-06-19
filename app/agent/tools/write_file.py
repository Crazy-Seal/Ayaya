"""write_file 工具 - 在工作区内写入文件"""

from typing import Annotated

from app.agent.tools.decorator import tool
from app.agent.utils.infra.safe_path import safe_path


@tool
async def write_file(
    path: Annotated[str, "被写入的文件路径字符串。"],
    content: Annotated[str, "要写入文件的内容字符串。"],
) -> str:
    """在指定路径写入内容，如果文件不存在则创建。会覆盖原有内容。"""
    try:
        fp = safe_path(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content, encoding="utf-8")
        return f"在{path}中写入了{len(content)}字节的内容。"
    except Exception as e:
        return f"错误: {e}"
