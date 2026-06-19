"""read_file 工具 - 读取工作区内文件内容"""

from typing import Annotated

from app.agent.tools.decorator import tool
from app.agent.utils.infra.safe_path import safe_path


@tool
async def read_file(
    path: Annotated[str, "被读取的文件路径字符串。"],
    limit: Annotated[int | None, "可选参数，最多返回的行数。"] = None,
) -> str:
    """读取指定路径的文件内容，并返回字符串结果。最多返回50000字符。"""
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
