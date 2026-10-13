"""read_image 工具 - 读取工作区图片并作为普通工具图片返回。"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Annotated

from app.agent.tools.base import ToolResult
from app.agent.tools.decorator import tool
from app.agent.tools.result_limits import MAX_IMAGE_BYTES
from app.agent.utils.infra.safe_path import safe_path


_MIME_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


@tool(compressible=True)
async def read_image(
    paths: Annotated[list[str], "要读取的工作区图片路径列表。"],
) -> ToolResult:
    """读取 PNG、JPEG 或 WebP 图片，并以 system_tool_image 类别提供给模型。

    Args:
        paths (list[str]): 要读取的工作区图片路径列表。
    """
    if not paths:
        return ToolResult(content="错误: paths 不能为空")

    images: list[str] = []
    failures: list[str] = []
    for path_value in paths:
        try:
            path = safe_path(path_value)
            mime_type = _MIME_BY_SUFFIX.get(Path(path_value).suffix.lower())
            if mime_type is None:
                raise ValueError("仅支持 PNG、JPEG 和 WebP")
            if path.stat().st_size > MAX_IMAGE_BYTES:
                raise ValueError("单张图片超过限制")
            encoded = base64.b64encode(path.read_bytes()).decode("ascii")
            images.append(f"data:{mime_type};base64,{encoded}")
        except Exception as exc:
            failures.append(f"{path_value}: {exc}")

    summary = f"已读取 {len(images)} 张图片"
    if failures:
        summary += "\n\n[未读取的图片：" + "；".join(failures) + "]"
    return ToolResult.success(
        summary,
        image_urls=images,
        media_kind="tool",
    )
