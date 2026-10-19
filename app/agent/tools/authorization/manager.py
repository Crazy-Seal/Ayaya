"""统一工具策略解析和一次调用授权。"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Callable, TYPE_CHECKING

from app.schemas.runtime_types import (
    ApprovalDecisionKind, InterruptKind,
)
from app.agent.state.messages import to_json_value
from app.agent.tools.core.base import InterruptEvent, ToolResult
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
    """将参数或准备动作按稳定键序编码并计算摘要，用于确认审批后的执行内容未变化。

    Args:
        value (Any): 当前需要校验或转换的值。

    Returns:
        str: 当前内容的 SHA-256 摘要。
    """
    encoded = json.dumps(
        to_json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def descriptor_for_tool(tool: Any) -> ToolDescriptor:
    """从统一 BaseTool 表面构造授权描述符。

    Args:
        tool (Any): 需要注册或执行的工具对象。
    """
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
        """保存最新配置读取、目录与配置写入依赖，工具调用时再核对权限和审批。

        Args:
            settings_loader (Callable[[str], ChatSettings]): 按会话编号读取最新 ChatSettings 的函数。
            catalog (UnifiedToolCatalog | None): 合并本地和 MCP 工具信息的目录对象。
            settings_writer (Callable[[str, 'ChatSettings'], 'ChatSettings'] | None): 保存会话设置并清理相关缓存的函数。
            mutation_coordinator (SettingsMutationCoordinator | None): 保证关联配置修改按顺序完成的协调器。
        """
        self._settings_loader = settings_loader
        self._catalog = catalog
        self._settings_writer = settings_writer
        self._mutation_coordinator = mutation_coordinator

    def list_policies(self, session_id: str) -> list["ToolCatalogEntry"]:
        """通过统一目录列出当前会话全部已知工具及其策略。

        Args:
            session_id (str): 当前操作所属的会话编号。
        """
        if self._catalog is None:
            raise RuntimeError("当前 ToolAuthorization 未配置工具目录")
        return self._catalog.list_entries(self._settings_loader(session_id))

    async def set_policy(
        self,
        session_id: str,
        identity: ToolIdentity,
        policy: ToolPolicy,
    ) -> list["ToolCatalogEntry"]:
        """更新一个工具策略并返回最新目录。

        Args:
            session_id (str): 当前操作所属的会话编号。
            identity (ToolIdentity): 来源、名称及服务编号确定的工具身份。
            policy (ToolPolicy): 该工具的 allow、ask 或 deny 权限。
        """
        return await self.set_policies(session_id, {identity.stable_key: policy})

    async def set_policies(
        self,
        session_id: str,
        policies: dict[str, ToolPolicy],
    ) -> list["ToolCatalogEntry"]:
        """原子更新一组工具策略；未知 stable key 一律拒绝。

        Args:
            session_id (str): 当前操作所属的会话编号。
            policies (dict[str, ToolPolicy]): 按 local:名称 或 mcp:服务编号:名称 保存的工具权限。
        """
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
        """按调用读取最新策略；配置异常安全降级为 DENY。

        Args:
            session_id (str): 当前操作所属的会话编号。
            identity (ToolIdentity): 来源、名称及服务编号确定的工具身份。
        """
        try:
            settings = self._settings_loader(session_id)
            if identity.source == "mcp":
                server = settings.mcp.servers.get(identity.server_id or "")
                if server is None or not server.enabled:
                    return ToolPolicy.DENY
            policy = settings.tool_permissions.get(identity.stable_key)
            # 1. 内存设置和测试注入也遵循权限改名规则，不依赖 YAML 已经完成补齐。
            if policy is None and identity.source == "local":
                legacy = settings.tool_permissions.get("local:run_ps")
                if identity.name == "exec_command" and legacy is not None:
                    policy = legacy
                elif identity.name == "write_stdin" and legacy == ToolPolicy.DENY:
                    policy = ToolPolicy.DENY
            return ToolPolicy(policy if policy is not None else ToolPolicy.ASK)
        except Exception:
            return ToolPolicy.DENY

    def is_exposed(self, session_id: str, tool: Any) -> bool:
        """生成模型工具列表时排除 deny 工具，ask 工具仍可公开但执行前需审批。

        Args:
            session_id (str): 当前操作所属的会话编号。
            tool (Any): 需要注册或执行的工具对象。

        Returns:
            bool: 当前权限不是 deny 时为 True。
        """
        return self.get_policy(session_id, descriptor_for_tool(tool).identity) != ToolPolicy.DENY

    def get_bound_mcp_identity(self, session_id: str, server_id: str) -> str | None:
        """读取已启用 MCP Server 的会话绑定身份。

        Args:
            session_id (str): 当前操作所属的会话编号。
            server_id (str): MCP 服务的稳定编号。
        """
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
        """校验 continuation 携带的批准快照仍绑定当前调用。

        Args:
            invocation (ToolInvocation): 当前工具、参数和准备动作。
            grant (dict[str, Any] | None): 单次授权快照；允许策略可不携带快照。

        Returns:
            bool: 当前权限与授权快照均允许继续执行。
        """
        # 1. 先读取最新策略，已撤销的权限不能靠旧授权继续执行。
        policy = self.get_policy(invocation.session_id, invocation.descriptor.identity)
        if policy == ToolPolicy.DENY:
            return False
        # 2. 未带 grant 时沿用自动允许或准备阶段规则，不把它当作人工批准。
        if grant is None:
            return (
                policy == ToolPolicy.ALLOW
                or (
                    invocation.descriptor.authorization_timing == "after_prepare"
                    and invocation.prepared_action is None
                )
            )
        # 3. 人工授权需同时匹配调用身份、参数、契约及具体准备动作。
        return (
            grant.get("decision") == ApprovalDecisionKind.APPROVE
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
        """按最新策略判断一个尚未产生用户决定的工具调用。

        Args:
            invocation (ToolInvocation): 当前调用的工具、参数与可选准备动作。

        Returns:
            AuthorizationOutcome: 已允许、待人工审批或按原规则拒绝的结果。
        """
        # 1. 禁止策略立即拒绝；动作准备阶段沿用既有授权时机。
        policy = self.get_policy(invocation.session_id, invocation.descriptor.identity)
        if policy == ToolPolicy.DENY:
            return ToolResult(content="错误: 当前工具不允许使用")

        if (
            invocation.descriptor.authorization_timing == "after_prepare"
            and invocation.prepared_action is None
        ):
            return Granted()

        # 2. 自动允许不创建审批请求；其他策略冻结调用信息后等待真人决定。
        if policy == ToolPolicy.ALLOW:
            return Granted()

        snapshot = self._snapshot(invocation)
        interrupt = InterruptEvent.create(
            type=InterruptKind.AUTHORIZATION_REQUEST,
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
        """校验原审批快照，并消费用户对该请求作出的明确决定。

        Args:
            invocation (ToolInvocation): 恢复时的实际工具调用。
            decision (dict[str, Any]): 已知用户决定，拒绝或非批准继续沿原分支处理。
            authorization_snapshot (dict[str, Any]): 创建审批时冻结的身份与摘要。

        Returns:
            AuthorizationOutcome: 绑定当前调用的批准快照或原有拒绝结果。
        """
        # 1. 审批快照先匹配当前调用，过期决定不能被重用。
        if not self._matches(invocation, authorization_snapshot):
            return ToolResult(content="错误: 工具审批已过期，请重新发起调用")
        # 2. 只有明确批准能发放 grant，拒绝保留原提示，其他决定按原过期路径处理。
        kind = decision.get("kind")
        if kind == ApprovalDecisionKind.REJECT:
            return ToolResult(content="工具调用已被用户拒绝")
        if kind != ApprovalDecisionKind.APPROVE:
            return ToolResult(content="错误: 工具审批已过期，请重新发起调用")

        # 3. 发放前再次检查最新权限，撤销优先于已有用户批准。
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
        """为一次调用保存身份、参数、工具定义和准备动作摘要，将批准绑定到具体操作。

        Args:
            invocation (ToolInvocation): 本次会话中的工具身份、调用参数及可选准备动作。

        Returns:
            dict[str, Any]: 可持久保存并在恢复执行时重新比对的调用摘要。
        """
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
        """恢复审批后重算调用摘要，确认批准对应的工具、参数和动作均未变化。

        Args:
            invocation (ToolInvocation): 本次会话中的工具身份、调用参数及可选准备动作。
            snapshot (dict[str, Any]): 审批时保存的工具调用与授权约束摘要。

        Returns:
            bool: 除保留审批编号外所有字段一致时为 True。
        """
        return snapshot == {
            **cls._snapshot(invocation),
            "request_id": snapshot.get("request_id"),
        }
