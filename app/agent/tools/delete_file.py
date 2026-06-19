"""delete_file 工具 - 删除工作区内文件"""

from typing import Annotated

from app.agent.tools.decorator import tool
from app.agent.utils.infra.safe_path import safe_path


@tool
async def delete_file(
    path: Annotated[str, "被删除的文件/文件夹路径字符串。"],
) -> str:
    """删除指定路径的文件或文件夹。"""
    try:
        fp = safe_path(path)
        if not fp.exists():
            return f"错误: {path}不存在。"
        fp.unlink()
        return f"已删除{path}"
    except Exception as e:
        return f"错误: {e}"
