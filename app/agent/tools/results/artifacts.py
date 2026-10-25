"""工具结果 artifact 的持久化与 URI 解析。"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, unquote
from uuid import uuid4

from app.schemas.tool_types import TOOL_RESULT_URI_PREFIX
from app.environment import get_tool_results_dir


_ARTIFACT_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


@dataclass(frozen=True)
class ToolResultArtifact:
    """描述已外置完整工具结果的会话内引用和本地保存位置。"""

    artifact_id: str
    uri: str
    path: Path


class ToolResultArtifactStore:
    """按会话保存完整工具结果，解析引用时禁止读取其他会话文件。"""

    def __init__(self, root: Path | None = None) -> None:
        """选择当前环境的完整工具结果根目录，构造时不创建文件。

        Args:
            root (Path | None): 超长工具结果的保存根目录，省略时使用当前环境目录。
        """
        self._root = (root or get_tool_results_dir()).resolve()

    @staticmethod
    def _session_segment(session_id: str) -> str:
        """把会话编号转成路径和 URI 共用的编码片段，拒绝空编号。

        Args:
            session_id (str): 当前操作所属的会话编号。

        Returns:
            str: 经 URL 编码的会话目录名。

        Raises:
            ValueError: 会话编号为空。
        """
        if not session_id:
            raise ValueError("session_id 不能为空")
        return quote(session_id, safe="-._~")

    def save(self, session_id: str, content: str) -> ToolResultArtifact:
        """原子保存完整文本并返回模型可读取的 URI。

        Args:
            session_id (str): 当前操作所属的会话编号。
            content (str): 当前处理的正文。
        """
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

        uri = f"{TOOL_RESULT_URI_PREFIX}{session_segment}/{artifact_id}"
        return ToolResultArtifact(artifact_id=artifact_id, uri=uri, path=path)

    def resolve(self, session_id: str, uri: str) -> Path:
        """把当前会话的 artifact URI 解析为受控本地路径。

        Args:
            session_id (str): 当前操作所属的会话编号。
            uri (str): 完整工具结果的 tool-result:// 会话内引用。
        """
        if not isinstance(uri, str) or not uri.startswith(TOOL_RESULT_URI_PREFIX):
            raise ValueError("不是有效的 tool-result URI")
        remainder = uri[len(TOOL_RESULT_URI_PREFIX):]
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
