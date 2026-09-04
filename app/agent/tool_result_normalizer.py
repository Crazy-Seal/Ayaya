"""工具结果进入运行状态前的统一文本与图片规范化。"""

from __future__ import annotations

import base64
import binascii
import re
from collections import Counter
from dataclasses import replace

from app.agent.context import ToolResult
from app.mcp.limits import (
    ALLOWED_IMAGE_MIME_TYPES,
    MAX_IMAGE_BYTES,
    MAX_IMAGES_PER_CALL,
    MAX_TOTAL_IMAGE_BYTES,
)


TOOL_RESULT_TRUNCATED_MARKER = "\n\n[工具结果过长，已截断中间部分]\n\n"


def char_weight(char: str) -> float:
    """返回单个 Unicode code point 的工具文本权重。"""
    return 0.25 if ord(char) < 128 else 1.0


def weighted_char_count(value: str) -> float:
    """计算工具文本的加权字符数。"""
    return sum(char_weight(char) for char in value)


def _take_weighted_prefix(value: str, budget: float) -> str:
    used = 0.0
    end = 0
    for end, char in enumerate(value, start=1):
        weight = char_weight(char)
        if used + weight > budget:
            return value[: end - 1]
        used += weight
    return value[:end]


def _take_weighted_suffix(value: str, budget: float) -> str:
    used = 0.0
    start = len(value)
    for start in range(len(value) - 1, -1, -1):
        weight = char_weight(value[start])
        if used + weight > budget:
            return value[start + 1 :]
        used += weight
    return value[start:]


def truncate_weighted_text(
    value: str,
    *,
    max_weight: float = 10000,
    head_weight: float = 8000,
    tail_weight: float = 2000,
) -> str:
    """按权重保留原文头尾，结构提示不占原文额度。"""
    if head_weight + tail_weight > max_weight:
        raise ValueError("头尾权重之和不能超过总权重")
    if weighted_char_count(value) <= max_weight:
        return value
    return (
        _take_weighted_prefix(value, head_weight)
        + TOOL_RESULT_TRUNCATED_MARKER
        + _take_weighted_suffix(value, tail_weight)
    )


def _has_valid_signature(mime_type: str, data: bytes) -> bool:
    if mime_type == "image/png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if mime_type == "image/jpeg":
        return data.startswith(b"\xff\xd8\xff")
    if mime_type == "image/webp":
        return len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP"
    return False


_DATA_URL_PATTERN = re.compile(
    r"^data:(?P<mime>[^;,]+);base64,(?P<data>.*)$",
    flags=re.DOTALL,
)


class ToolResultNormalizer:
    """规范化内置工具和已适配 MCP 工具的最终 ``ToolResult``。"""

    def normalize(self, result: ToolResult) -> ToolResult:
        content = truncate_weighted_text(str(result.content or ""))
        candidates = list(result.image_urls)
        if result.image_url and result.image_url not in candidates:
            candidates.insert(0, result.image_url)

        accepted: list[str] = []
        ignored: Counter[str] = Counter()
        total_bytes = 0
        for candidate in candidates:
            if len(accepted) >= MAX_IMAGES_PER_CALL:
                ignored["图片数量超过限制"] += 1
                continue
            match = _DATA_URL_PATTERN.match(candidate) if isinstance(candidate, str) else None
            if match is None:
                ignored["必须使用完整 data URL"] += 1
                continue
            mime_type = match.group("mime").lower()
            encoded = match.group("data")
            if mime_type not in ALLOWED_IMAGE_MIME_TYPES:
                ignored["MIME 不支持"] += 1
                continue
            # Base64 每四个字符至多表示三个字节，预检避免先分配超大对象。
            if (len(encoded) * 3) // 4 > MAX_IMAGE_BYTES:
                ignored["单张图片超过限制"] += 1
                continue
            try:
                decoded = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError):
                ignored["Base64 非法"] += 1
                continue
            if len(decoded) > MAX_IMAGE_BYTES:
                ignored["单张图片超过限制"] += 1
                continue
            if total_bytes + len(decoded) > MAX_TOTAL_IMAGE_BYTES:
                ignored["图片总量超过限制"] += 1
                continue
            if not _has_valid_signature(mime_type, decoded):
                ignored["文件签名不匹配"] += 1
                continue
            total_bytes += len(decoded)
            accepted.append(candidate)

        for reason, count in ignored.items():
            separator = "\n\n" if content else ""
            content += f"{separator}[已忽略 {count} 张工具图片：{reason}]"

        return replace(
            result,
            content=content,
            image_url=accepted[0] if accepted else None,
            image_urls=accepted,
        )
