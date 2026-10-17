"""read_file 工具 - 按范围读取工作区文件或工具结果 artifact。"""

from pathlib import Path
import asyncio
from typing import Annotated, Literal

from app.agent.tools.core.base import ToolContext
from app.agent.tools.core.decorator import tool
from app.agent.tools.results.artifacts import ToolResultArtifactStore
from app.agent.utils.infra.safe_path import safe_path


def _read_task_page(path: Path, start: int, end: int | None) -> str:
    """有界读取任务日志字符页，不将超长单行或整个日志载入内存。

    Args:
        path (Path): 已确认属于当前聊天的任务日志。
        start (int): 从零开始的字符位置。
        end (int | None): 不包含的结束位置；省略时读取一页。

    Returns:
        str: 有限正文及下一次字符读取位置。
    """
    # 1. 逐块跳过前文，每次最多持有一块；最多返回 6000 个字符，避免再次裁剪页码提示。
    limit = min(6000, max(0, end - start)) if end is not None else 6000
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        remaining = start
        while remaining:
            chunk = stream.read(min(remaining, 8192))
            if not chunk:
                return "(已到达日志末尾)"
            remaining -= len(chunk)
        text = stream.read(limit)
        has_more = bool(stream.read(1))
    # 2. 页码基于完整字符，换行和终端控制字符也计入，不依赖模型已读位置。
    next_start = start + len(text)
    hint = f"[next_start: {next_start}; unit: character; has_more: {str(has_more).lower()}]"
    return hint + "\n" + text


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
    path: Annotated[str, "被读取的文件路径、tool-result URI 或完成通知中的 task-output URI。"],
    start: Annotated[int | None, "可选起始位置，从 0 开始且包含。"] = None,
    end: Annotated[int | None, "可选结束位置，不包含。"] = None,
    unit: Annotated[Literal["line", "character"], "范围单位：line 或 character。"] = "line",
    context: ToolContext | None = None,
) -> str:
    """读取 UTF-8 文本；普通文件支持行或字符范围，task-output 日志固定按字符分页。

    Args:
        path (str): 普通路径、tool-result URI 或 task-output URI。
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

        # 1. 任务日志先校验聊天归属，再在线程中流式读取；该 URI 的 start/end 始终是字符位置。
        if path.startswith("task-output://"):
            if context.task_launcher is None:
                raise ValueError("任务日志读取器未就绪")
            resolved = await context.task_launcher.resolve_output(context.session_id, path)
            return await asyncio.to_thread(_read_task_page, resolved, normalized_start, end)

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
