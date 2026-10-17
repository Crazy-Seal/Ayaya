"""将 MCP 工具目录适配到 Ayaya 的统一 ToolManager。"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, TYPE_CHECKING

from app.agent.state.messages import TextContent, ImageContent, content_text
from app.agent.tools.core.base import BaseTool, ToolContext, ToolResult
from app.agent.tools.authorization.manager import ToolAuthorization
from app.agent.tools.authorization.models import ToolIdentity
from app.mcp.identity import tool_contract_fingerprint
from app.mcp.types import MCPToolDescriptor
from app.mcp.limits import (
    MAX_CONTENT_PARTS,
    MAX_SCHEMA_DEPTH,
    MAX_SCHEMA_NODES,
    validate_json_value,
)
if TYPE_CHECKING:
    from app.services.mcp_connection_manager import MCPConnectionManager


def build_exposed_tool_name(server_id: str, remote_tool_name: str) -> str:
    """生成唯一且符合模型 API 限制的 MCP 工具名称。

    Args:
        server_id (str): MCP Server ID。
        remote_tool_name (str): Server 中的原始工具名称。

    Returns:
        str: 带 Server 命名空间、最长 64 字符的模型可见名称。
    """
    raw = f"mcp__{server_id}__{remote_tool_name}"
    normalized = re.sub(r"[^A-Za-z0-9_-]", "_", raw)
    if normalized == raw and len(normalized) <= 64:
        return normalized
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
    return f"{normalized[:54]}__{digest}"[:64]


class MCPToolAdapter(BaseTool):
    """把单个 MCP 工具适配到 Ayaya 的统一工具协议。"""

    def __init__(
        self,
        *,
        session_id: str,
        server_id: str,
        server_name: str,
        descriptor: MCPToolDescriptor,
        connection_manager: "MCPConnectionManager",
        authorization: ToolAuthorization,
    ):
        """初始化 MCP 工具适配器。

        Args:
            session_id (str): 模型配置对应的会话 ID。
            server_id (str): MCP Server ID。
            server_name (str): MCP Server 显示名称。
            descriptor (MCPToolDescriptor): 远端工具契约描述符。
            connection_manager (MCPConnectionManager): 进程级 MCP 连接管理器。
            authorization (ToolAuthorization): 统一工具授权层。
        """
        self.session_id = session_id
        self.server_id = server_id
        self.server_name = server_name
        self.remote_tool_name = descriptor.name
        self.identity = ToolIdentity.mcp(server_id, descriptor.name)
        self.name = build_exposed_tool_name(server_id, descriptor.name)
        self.description = f"[MCP: {server_name}] {descriptor.description}".strip()
        self.parameters_schema = descriptor.input_schema or {"type": "object", "properties": {}}
        self.tool_contract_fingerprint = tool_contract_fingerprint(descriptor)
        self.server_identity_fingerprint = connection_manager.get_server_identity(server_id)
        self.authorization_contract_fingerprint = hashlib.sha256(
            (
                f"{self.server_identity_fingerprint}\0"
                f"{self.tool_contract_fingerprint}"
            ).encode("utf-8")
        ).hexdigest()
        self.compressible = (descriptor.annotations or {}).get("readOnlyHint") is True
        self._connection_manager = connection_manager
        self._authorization = authorization

    async def execute(self, args: dict, context: ToolContext) -> ToolResult:
        """校验远端身份和契约后执行 MCP 工具调用。

        Args:
            args (dict): 模型生成的工具调用参数。
            context (ToolContext): 包含会话状态和中断恢复数据的工具上下文。

        Returns:
            ToolResult: MCP 执行结果或安全校验错误。
        """
        identity_error = self._validate_policy_identity()
        if identity_error:
            return ToolResult(content=f"错误: {identity_error}")

        try:
            result = await self._connection_manager.call_tool(
                self.server_id,
                self.remote_tool_name,
                args,
                execution_guard=self._execution_guard,
            )
        except Exception as exc:
            return ToolResult(content=f"错误: {exc}")
        return self._convert_result(result)

    def _execution_guard(self) -> str | None:
        """在获得调用租约前执行最终权限和契约校验。

        Args:
        Returns:
            str | None: 校验失败时的错误消息；通过时返回 ``None``。
        """
        if not self._authorization.is_exposed(self.session_id, self):
            return "当前模型未获准使用此 MCP 工具"
        identity_error = self._validate_policy_identity()
        if identity_error:
            return identity_error
        if self._connection_manager.get_tool_contract_fingerprint(
            self.server_id, self.remote_tool_name
        ) != self.tool_contract_fingerprint:
            return "MCP_APPROVAL_STALE: MCP 工具定义已变化，请重新发起调用并确认"
        return None

    def _validate_policy_identity(self) -> str | None:
        """校验实时会话权限是否绑定到当前 Server 身份。

        Returns:
            str | None: 身份缺失或发生变化时的错误消息；校验通过时返回 ``None``。
        """
        expected = self._authorization.get_bound_mcp_identity(
            self.session_id, self.server_id
        )
        current = self._connection_manager.get_server_identity(self.server_id)
        if (
            expected != self.server_identity_fingerprint
            or current != self.server_identity_fingerprint
        ):
            return "MCP_IDENTITY_UNBOUND: 当前模型权限尚未绑定到此 MCP 服务器身份"
        return None

    @staticmethod
    def _convert_result(result: Any) -> ToolResult:
        """把 MCP SDK 调用结果转换为 Ayaya 工具结果。

        Args:
            result (Any): MCP SDK 返回的原始调用结果。

        Returns:
            ToolResult: 包含文本、结构化内容和图片的统一工具结果。
        """
        # 1. 兼容两种 SDK 字段名称，结构化结果和文本/图片部分分别读取。
        structured = getattr(result, "structuredContent", None)
        if structured is None:
            structured = getattr(result, "structured_content", None)

        blocks = []
        unsupported_count = 0
        skipped_content_parts = 0
        content_parts = getattr(result, "content", None) or []
        try:
            content_part_count = len(content_parts)
        except TypeError:
            content_part_count = None
        # 2. 按服务返回顺序读取受支持内容，超过部分数上限即停止；图片格式校验由统一结果层完成。
        for index, part in enumerate(content_parts):
            if index >= MAX_CONTENT_PARTS:
                skipped_content_parts = (
                    max(1, content_part_count - index)
                    if content_part_count is not None
                    else 1
                )
                break
            part_type = getattr(part, "type", "unknown")
            if part_type == "text":
                text = getattr(part, "text", None)
                if text:
                    blocks.append(TextContent(str(text)))
            elif part_type == "image":
                data = getattr(part, "data", "")
                mime_value = getattr(part, "mimeType", getattr(part, "mime_type", ""))
                mime_type = mime_value if isinstance(mime_value, str) else ""
                encoded = data if isinstance(data, str) else str(data)
                blocks.append(ImageContent(f"data:{mime_type};base64,{encoded}", media_kind="mcp"))
            elif part_type != "text":
                unsupported_count += 1

        content = content_text(blocks)

        def append_section(value: str) -> None:
            """转换 MCP 结果时按出现顺序追加一段文本，已有正文时用空行分隔。

            Args:
                value (str): 当前需要校验或转换的值。
            """
            nonlocal content
            prefix = "\n" if content else ""
            content += ("\n\n" if content else "") + value
            blocks.append(TextContent(prefix + value))

        # 3. 把忽略原因加入正文，随后与普通工具结果共用同一文本限长流程。
        if unsupported_count:
            append_section(f"[已忽略 {unsupported_count} 个不支持的 MCP 内容类型]")
        if skipped_content_parts:
            append_section(
                f"[已忽略 {skipped_content_parts} 个 MCP content parts：超过总数限制]"
            )
        # 4. 结构化结果限制深度和节点数，不按 Schema 字节上限截断；已有完全相同文本则不重复追加。
        if structured is not None:
            try:
                validate_json_value(
                    structured,
                    label="MCP structured content",
                    max_depth=MAX_SCHEMA_DEPTH,
                    max_nodes=MAX_SCHEMA_NODES,
                    max_bytes=None,
                )
                structured_text = json.dumps(
                    structured,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    allow_nan=False,
                )
                if structured_text not in content:
                    append_section(
                        structured_text
                        if not content
                        else f"[structuredContent]\n{structured_text}"
                    )
            except (TypeError, ValueError) as exc:
                append_section(f"[已忽略 structured content: {exc}]")

        # 5. 服务的错误标记转成工具错误正文，完整结果继续交给管道统一规范化。
        content = content or "(MCP 工具未返回内容)"
        if bool(getattr(result, "isError", getattr(result, "is_error", False))):
            return ToolResult(content=f"错误: {content or 'MCP Server 返回错误'}")
        if not any(isinstance(block, TextContent) for block in blocks):
            blocks.insert(0, TextContent(content))
        return ToolResult(content=tuple(blocks))
