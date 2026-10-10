"""工具结果进入运行状态前的统一文本与图片规范化。"""

from __future__ import annotations

import base64
import binascii
import re
from collections import Counter
from dataclasses import replace

from app.agent.tools.base import ToolResult
from app.agent.tools.result_artifacts import ToolResultArtifactStore
from app.agent.tools.result_limits import (
    ALLOWED_IMAGE_MIME_TYPES,
    MAX_IMAGE_BYTES,
    MAX_IMAGES_PER_CALL,
    MAX_TOTAL_IMAGE_BYTES,
)
from app.agent.utils.domain.weighted_text import (
    WEIGHTED_TEXT_TRUNCATED_MARKER as TOOL_RESULT_TRUNCATED_MARKER,
    truncate_weighted_text,
    weighted_char_count,
)


MAX_TOOL_RESULT_BYTES = 10 * 1024 * 1024
TOOL_RESULT_TOO_LARGE_MARKER = (
    "\n\n[系统提示：工具返回结果超过 10 MiB 安全上限，未保存完整内容。 "
    "请缩小查询范围、增加过滤条件或让工具分页返回后重试。]\n\n"
)
TOOL_RESULT_READ_RANGE_MARKER = (
    "\n\n[系统提示：工具返回结果过长，中间部分已截断。"
    "该读取工具不会再次保存结果，请缩小读取范围后重试。]\n\n"
)


def _has_valid_signature(mime_type: str, data: bytes) -> bool:
    """核对工具图片字节的 PNG、JPEG 或 WebP 文件头，不能只信任 MIME 声明。

    Args:
        mime_type (str): 图片 data URL 声明的 MIME 类型。
        data (bytes): Base64 解码后的图片字节。

    Returns:
        bool: MIME 类型与支持的文件头一致时为 True。
    """
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

    def __init__(self, artifact_store: ToolResultArtifactStore | None = None) -> None:
        """保存可替换的完整结果存储，只有需要外置长结果时才使用。

        Args:
            artifact_store (ToolResultArtifactStore | None): 可注入的完整工具结果保存对象。
        """
        self._artifact_store = artifact_store

    def normalize(
        self,
        result: ToolResult,
        *,
        session_id: str = "default",
        persist_overflow: bool = True,
    ) -> ToolResult:
        """工具执行后统一校验图片数量、字节和格式，加入忽略原因再按文本预算生成结果副本。

        Args:
            result (ToolResult): 当前工具或被包装函数返回的结果。
            session_id (str): 当前操作所属的会话编号。
            persist_overflow (bool): 超过模型预览预算时是否允许保存完整结果。

        Returns:
            ToolResult: 保留有效图片并限制模型预览正文的 ToolResult，原结果对象不变。
        """
        content = str(result.content or "")
        candidates = list(result.image_urls)
        if result.image_url and result.image_url not in candidates:
            candidates.insert(0, result.image_url)

        # 1. 逐张检查图片，只有通过格式和字节限制的图片才占用数量及总大小预算。
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

        # 2. 先把被忽略图片的原因写入正文，再统一截断，确保诊断信息也参与文本预算。
        for reason, count in ignored.items():
            separator = "\n\n" if content else ""
            content += f"{separator}[已忽略 {count} 张工具图片：{reason}]"

        content = self._normalize_text(
            content,
            session_id=session_id,
            persist_overflow=persist_overflow,
        )

        return replace(
            result,
            content=content,
            image_url=accepted[0] if accepted else None,
            image_urls=accepted,
        )

    def _normalize_text(
        self,
        content: str,
        *,
        session_id: str,
        persist_overflow: bool,
    ) -> str:
        """长正文在允许范围内保存完整结果并生成首尾预览，超安全上限或读取类工具则只截断。

        Args:
            content (str): 当前处理的正文。
            session_id (str): 当前操作所属的会话编号。
            persist_overflow (bool): 超过模型预览预算时是否允许保存完整结果。

        Returns:
            str: 模型可读正文，按情况附完整结果引用或缩小读取范围提示。
        """
        if weighted_char_count(content) <= 10_000:
            return content
        # 1. 超过安全字节上限不保存全文；读取类工具也不另存结果，避免不断生成新引用。
        if len(content.encode("utf-8")) > MAX_TOOL_RESULT_BYTES:
            marker = TOOL_RESULT_TOO_LARGE_MARKER
        elif not persist_overflow:
            marker = TOOL_RESULT_READ_RANGE_MARKER
        else:
            # 2. 可以保存的长结果按会话外置，预览留下引用，模型可用 read_file 分段读取。
            store = self._artifact_store or ToolResultArtifactStore()
            reference = store.save(session_id, content)
            marker = (
                "\n\n[系统提示：工具返回结果过长，中间部分已截断。"
                f"完整内容已保存至 {reference.uri}；请使用 read_file 分段读取。]\n\n"
            )
        return truncate_weighted_text(content, marker=marker)
