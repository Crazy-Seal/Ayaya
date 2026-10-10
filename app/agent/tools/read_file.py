"""read_file 工具 - 按范围读取工作区文件或工具结果 artifact。"""

from pathlib import Path
from typing import Annotated, Literal

from app.agent.tools.base import ToolContext
from app.agent.tools.decorator import tool
from app.agent.tools.result_artifacts import ToolResultArtifactStore
from app.agent.utils.infra.safe_path import safe_path


def _resolve_path(path: str, context: ToolContext) -> Path:
    """区分当前会话工具结果引用和工作区路径，禁止跨会话结果读取及工作区越界。

    Args:
        path (str): 要读取或写入的文件路径。
        context (ToolContext): 执行管道提供的工具运行上下文。

    Returns:
        Path: 通过范围检查的本地文件路径。

    Raises:
        ValueError: 引用属于其他会话，或普通路径越过工作区。
    """
    if path.startswith("tool-result://"):
        return ToolResultArtifactStore().resolve(context.session_id, path)
    return safe_path(path)


@tool(compressible=True, persist_oversized_result=False)
async def read_file(
    path: Annotated[str, "被读取的文件路径或 tool-result URI。"],
    start: Annotated[int | None, "可选起始位置，从 0 开始且包含。"] = None,
    end: Annotated[int | None, "可选结束位置，不包含。"] = None,
    unit: Annotated[Literal["line", "character"], "范围单位：line 或 character。"] = "line",
    context: ToolContext | None = None,
) -> str:
    """读取 UTF-8 文本；可按从 0 开始的半开行范围或字符范围读取。

    Args:
        path (str): 被读取的文件路径或 tool-result URI。
        start (int | None): 可选起始位置，从 0 开始且包含。
        end (int | None): 可选结束位置，不包含。
        unit (Literal['line', 'character']): 范围单位：line 或 character。
        context (ToolContext | None): 执行管道提供的工具运行上下文。
    """
    try:
        if context is None:
            raise ValueError("缺少工具上下文")
        normalized_start = 0 if start is None else start
        if normalized_start < 0 or (end is not None and end < normalized_start):
            raise ValueError("读取范围无效：必须满足 0 <= start <= end")

        # utf-8-sig 同时兼容普通 UTF-8 与带 BOM 的文本。
        text = _resolve_path(path, context).read_text(encoding="utf-8-sig")
        if unit == "character":
            return text[normalized_start:end]
        if unit == "line":
            return "\n".join(text.splitlines()[normalized_start:end])
        raise ValueError("unit 必须是 line 或 character")
    except UnicodeDecodeError:
        return "错误: 文件不是UTF-8编码，请先转换为UTF-8后再读取。"
    except Exception as e:
        return f"错误: {e}"
