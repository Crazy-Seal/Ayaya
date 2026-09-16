"""统一工具策略解析和一次调用授权。"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Callable, TYPE_CHECKING

from app.agent.tools.base import InterruptEvent, ToolResult
from app.schemas.tool_permissions import ToolPolicy

from .models import (
    AuthorizationGrant,
    AuthorizationOutcome,
    AuthorizationRequired,
    Granted,
    ToolDescriptor,
    ToolIdentity,
    ToolInvocation,
)

if TYPE_CHECKING:
    from app.schemas.chat_settings import ChatSettings
    from app.services.settings_mutation import SettingsMutationCoordinator

    from .catalog import ToolCatalogEntry, UnifiedToolCatalog


def _fingerprint(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def descriptor_for_tool(tool: Any) -> ToolDescriptor:
    """从统一 BaseTool 表面构造授权描述符。"""
    identity = getattr(tool, "identity", None) or ToolIdentity.local(tool.name)
    schema = getattr(tool, "parameters_schema", {}) or {}
    contract = (
        getattr(tool, "authorization_contract_fingerprint", None)
        or getattr(tool, "tool_contract_fingerprint", None)
        or _fingerprint({
            "identity": identity.stable_key,
            "exposed_name": tool.name,
            "display_name": getattr(tool, "display_name", None) or tool.name,
            "description": getattr(tool, "description", "") or "",
            "input_schema": schema,
            "authorization_timing": getattr(
                tool, "authorization_timing", "before_execute"
            ),
        })
    )
    return ToolDescriptor(
        identity=identity,
        exposed_name=tool.name,
        display_name=getattr(tool, "display_name", None) or tool.name,
        description=getattr(tool, "description", "") or "",
        input_schema=schema,
        contract_fingerprint=contract,
        authorization_timing=getattr(tool, "authorization_timing", "before_execute"),
    )


class ToolAuthorization:
    """集中实现本地与 MCP 工具共享的三态授权语义。"""

    def __init__(
        self,
        settings_loader: Callable[[str], ChatSettings],
        *,
        catalog: "UnifiedToolCatalog | None" = None,
        settings_writer: Callable[[str, "ChatSettings"], "ChatSettings"] | None = None,
        mutation_coordinator: "SettingsMutationCoordinator | None" = None,
    ) -> None:
        self._settings_loader = settings_loader
        self._catalog = catalog
        self._settings_writer = settings_writer
        self._mutation_coordinator = mutation_coordinator

    def list_policies(self, session_id: str) -> list["ToolCatalogEntry"]:
        """通过统一目录列出当前会话全部已知工具及其策略。"""
        if self._catalog is None:
            raise RuntimeError("当前 ToolAuthorization 未配置工具目录")
        return self._catalog.list_entries(self._settings_loader(session_id))

    async def set_policy(
        self,
        session_id: str,
        identity: ToolIdentity,
        policy: ToolPolicy,
    ) -> list["ToolCatalogEntry"]:
        """更新一个工具策略并返回最新目录。"""
        return await self.set_policies(session_id, {identity.stable_key: policy})

    async def set_policies(
        self,
        session_id: str,
        policies: dict[str, ToolPolicy],
    ) -> list["ToolCatalogEntry"]:
        """原子更新一组工具策略；未知 stable key 一律拒绝。"""
        if self._settings_writer is None or self._mutation_coordinator is None:
            raise RuntimeError("当前 ToolAuthorization 未配置策略存储")
        async with self._mutation_coordinator.lock:
            settings = self._settings_loader(session_id)
            known = {item.stable_key for item in self.list_policies(session_id)}
            unknown = sorted(set(policies) - known)
            if unknown:
                raise ValueError(f"包含未知工具权限: {', '.join(unknown)}")
            merged = dict(settings.tool_permissions)
            merged.update(policies)
            self._settings_writer(
                session_id,
                settings.model_copy(update={"tool_permissions": merged}),
            )
        return self.list_policies(session_id)

    def get_policy(self, session_id: str, identity: ToolIdentity) -> ToolPolicy:
        """按调用读取最新策略；配置异常安全降级为 DENY。"""
        try:
            settings = self._settings_loader(session_id)
            if identity.source == "mcp":
                server = settings.mcp.servers.get(identity.server_id or "")
                if server is None or not server.enabled:
                    return ToolPolicy.DENY
            return ToolPolicy(
                settings.tool_permissions.get(identity.stable_key, ToolPolicy.ASK)
            )
        except Exception:
            return ToolPolicy.DENY

    def is_exposed(self, session_id: str, tool: Any) -> bool:
        return self.get_policy(session_id, descriptor_for_tool(tool).identity) != ToolPolicy.DENY

    def get_bound_mcp_identity(self, session_id: str, server_id: str) -> str | None:
        """读取已启用 MCP Server 的会话绑定身份。"""
        try:
            server = self._settings_loader(session_id).mcp.servers.get(server_id)
            return server.identity_fingerprint if server and server.enabled else None
        except Exception:
            return None

    def validate_grant(
        self,
        invocation: ToolInvocation,
        grant: dict[str, Any] | None,
    ) -> bool:
        """校验 continuation 携带的批准快照仍绑定当前调用。"""
        policy = self.get_policy(invocation.session_id, invocation.descriptor.identity)
        if policy == ToolPolicy.DENY:
            return False
        if grant is None:
            return (
                policy == ToolPolicy.ALLOW
                or (
                    invocation.descriptor.authorization_timing == "after_prepare"
                    and invocation.prepared_action is None
                )
            )
        return (
            grant.get("decision") == "approve"
            and grant.get("tool_call_id") == invocation.tool_call_id
            and grant.get("tool_identity") == invocation.descriptor.identity.stable_key
            and grant.get("arguments_fingerprint") == _fingerprint(invocation.arguments)
            and grant.get("contract_fingerprint") == invocation.descriptor.contract_fingerprint
            and grant.get("prepared_action_fingerprint")
            == (
                _fingerprint(invocation.prepared_action)
                if invocation.prepared_action is not None
                else None
            )
        )

    def authorize(
        self,
        invocation: ToolInvocation,
    ) -> AuthorizationOutcome:
        """按最新策略判断一个尚未产生用户决定的工具调用。"""
        policy = self.get_policy(invocation.session_id, invocation.descriptor.identity)
        if policy == ToolPolicy.DENY:
            return ToolResult(content="错误: 当前工具不允许使用")

        if (
            invocation.descriptor.authorization_timing == "after_prepare"
            and invocation.prepared_action is None
        ):
            return Granted()

        if policy == ToolPolicy.ALLOW:
            return Granted()

        snapshot = self._snapshot(invocation)
        interrupt = InterruptEvent.create(
            type="authorization_request",
            message=f"Agent 请求调用工具 {invocation.descriptor.display_name}",
            data={
                "tool_call_id": invocation.tool_call_id,
                "tool_identity": invocation.descriptor.identity.stable_key,
                "tool_name": invocation.descriptor.display_name,
                "source": invocation.descriptor.identity.source,
                "arguments": invocation.arguments,
                "prepared_action": invocation.prepared_action,
                "contract_fingerprint": invocation.descriptor.contract_fingerprint,
            },
        )
        snapshot["request_id"] = interrupt.request_id
        return AuthorizationRequired(interrupt=interrupt, resume_state=snapshot)

    def resolve_authorization(
        self,
        invocation: ToolInvocation,
        *,
        decision: dict[str, Any],
        authorization_snapshot: dict[str, Any],
    ) -> AuthorizationOutcome:
        """校验原审批快照，并消费用户对该请求作出的明确决定。"""
        if not self._matches(invocation, authorization_snapshot):
            return ToolResult(content="错误: 工具审批已过期，请重新发起调用")
        kind = decision.get("kind")
        if kind == "reject":
            return ToolResult(content="工具调用已被用户拒绝")
        if kind != "approve":
            return ToolResult(content="错误: 工具审批已过期，请重新发起调用")

        policy = self.get_policy(invocation.session_id, invocation.descriptor.identity)
        if policy == ToolPolicy.DENY:
            return ToolResult(content="错误: 当前工具不允许使用")

        return Granted(
            AuthorizationGrant(
                request_id=str(authorization_snapshot.get("request_id") or ""),
                tool_call_id=invocation.tool_call_id,
                tool_identity=invocation.descriptor.identity.stable_key,
                arguments_fingerprint=_fingerprint(invocation.arguments),
                contract_fingerprint=invocation.descriptor.contract_fingerprint,
                prepared_action_fingerprint=(
                    _fingerprint(invocation.prepared_action)
                    if invocation.prepared_action is not None
                    else None
                ),
                issued_at=datetime.now(timezone.utc).isoformat(),
            )
        )

    @staticmethod
    def _snapshot(invocation: ToolInvocation) -> dict[str, Any]:
        return {
            "tool_call_id": invocation.tool_call_id,
            "tool_identity": invocation.descriptor.identity.stable_key,
            "arguments_fingerprint": _fingerprint(invocation.arguments),
            "contract_fingerprint": invocation.descriptor.contract_fingerprint,
            "prepared_action_fingerprint": (
                _fingerprint(invocation.prepared_action)
                if invocation.prepared_action is not None
                else None
            ),
        }

    @classmethod
    def _matches(cls, invocation: ToolInvocation, snapshot: dict[str, Any]) -> bool:
        return snapshot == {
            **cls._snapshot(invocation),
            "request_id": snapshot.get("request_id"),
        }
