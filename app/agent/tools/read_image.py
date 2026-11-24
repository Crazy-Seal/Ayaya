"""read_image 工具 - 读取工作区或图片目录中的图片并作为普通工具图片返回。"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Annotated

from app.schemas.message_types import MediaKind
from app.agent.tools.core.base import ToolResult
from app.agent.tools.core.decorator import tool
from app.agent.tools.results.limits import MAX_IMAGE_BYTES
from app.agent.utils.domain.images import IMAGE_URI_PREFIX, resolve_image_path
from app.agent.utils.infra.safe_path import safe_path


_MIME_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


def _resolve_path(path_value: str) -> Path:
    """把读取地址解析为工作区或图片目录中的实际文件。

    Args:
        path_value (str): 工作区相对路径，或以 image:// 开头的图片存储引用。

    Returns:
        Path: 已校验的绝对路径。

    Raises:
        ValueError: 地址越过允许的目录，或图片引用格式无效。
    """
    # 1. image:// 引用只指向图片存储目录；其余地址按 Agent 工作区解析。
    if path_value.startswith(IMAGE_URI_PREFIX):
        return resolve_image_path(path_value[len(IMAGE_URI_PREFIX):])
    return safe_path(path_value)


@tool(
    description=(
        "读取 PNG、JPEG 或 WebP 图片，并返回图片内容供你查看。"
        "地址可以是工作区相对路径，也可以是以 image:// 开头的图片存储引用。"
    ),
    compressible=True,
)
async def read_image(
    paths: Annotated[list[str], "要读取的图片路径列表：工作区相对路径，或以 image:// 开头的图片存储引用。"],
) -> ToolResult:
    """读取 PNG、JPEG 或 WebP 图片，并以 system_tool_image 类别提供给模型。

    Args:
        paths (list[str]): 要读取的图片路径列表。
    """
    if not paths:
        return ToolResult(content="错误: paths 不能为空")

    images: list[str] = []
    failures: list[str] = []
    for path_value in paths:
        try:
            path = _resolve_path(path_value)
            mime_type = _MIME_BY_SUFFIX.get(path.suffix.lower())
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
        media_kind=MediaKind.TOOL,
    )
