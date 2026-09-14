"""工具结果 artifact 的持久化与 URI 解析。"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, unquote
from uuid import uuid4

from app.runtime import get_tool_results_dir


_ARTIFACT_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_URI_PREFIX = "tool-result://"


@dataclass(frozen=True)
class ToolResultArtifact:
    """已保存工具结果的稳定引用。"""

    artifact_id: str
    uri: str
    path: Path


class ToolResultArtifactStore:
    """在会话隔离目录中保存并解析工具结果。"""

    def __init__(self, root: Path | None = None) -> None:
        self._root = (root or get_tool_results_dir()).resolve()

    @staticmethod
    def _session_segment(session_id: str) -> str:
        if not session_id:
            raise ValueError("session_id 不能为空")
        return quote(session_id, safe="-._~")

    def save(self, session_id: str, content: str) -> ToolResultArtifact:
        """原子保存完整文本并返回模型可读取的 URI。"""
        session_segment = self._session_segment(session_id)
        artifact_id = uuid4().hex
        session_dir = (self._root / session_segment).resolve()
        if not session_dir.is_relative_to(self._root):
            raise ValueError("工具结果目录越界")
        session_dir.mkdir(parents=True, exist_ok=True)

        path = session_dir / f"{artifact_id}.txt"
        temporary = session_dir / f".{artifact_id}.tmp"
        try:
            temporary.write_text(content, encoding="utf-8")
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

        uri = f"{_URI_PREFIX}{session_segment}/{artifact_id}"
        return ToolResultArtifact(artifact_id=artifact_id, uri=uri, path=path)

    def resolve(self, session_id: str, uri: str) -> Path:
        """把当前会话的 artifact URI 解析为受控本地路径。"""
        if not isinstance(uri, str) or not uri.startswith(_URI_PREFIX):
            raise ValueError("不是有效的 tool-result URI")
        remainder = uri[len(_URI_PREFIX):]
        session_segment, separator, artifact_id = remainder.partition("/")
        if not separator or "/" in artifact_id:
            raise ValueError("tool-result URI 格式无效")
        if unquote(session_segment) != session_id:
            raise ValueError("tool-result URI 不属于当前会话")
        if not _ARTIFACT_ID_PATTERN.fullmatch(artifact_id):
            raise ValueError("artifact id 无效")

        path = (self._root / session_segment / f"{artifact_id}.txt").resolve()
        if not path.is_relative_to(self._root):
            raise ValueError("工具结果路径越界")
        return path


__all__ = ["ToolResultArtifact", "ToolResultArtifactStore"]
