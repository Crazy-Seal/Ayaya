"""读取会话工具权限，判断模型请求的调用能否执行，并核对用户批准的具体操作。"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Callable, TYPE_CHECKING

from app.schemas.tool_types import ToolSource, AuthorizationTiming
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


def _fingerprint(value: Any) -> str:
    """将参数或准备动作按稳定键序编码并计算摘要，用于确认审批后的执行内容未变化。

    Args:
        value (Any): 本次调用的参数、工具定义或准备好的屏幕动作。

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
    """读取工具的来源、名称、参数定义和审批时机，整理为权限检查所需的信息。

    ToolManager 筛选模型工具列表、Pipeline 检查一次工具调用时使用本函数。
    本地工具根据公开定义计算摘要；MCP 工具提供的摘要还包含所连接服务的身份，
    用于发现用户批准后工具或服务已经发生变化。

    Args:
        tool (Any): 当前已注册的本地工具或 MCP 工具适配器。

    Returns:
        ToolDescriptor: 区分工具来源、判断审批时机及比较工具定义所需的信息。
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
                tool, "authorization_timing", AuthorizationTiming.BEFORE_EXECUTE
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
        authorization_timing=getattr(tool, "authorization_timing", AuthorizationTiming.BEFORE_EXECUTE),
    )


class ToolAuthorization:
    """按用户已保存的工具权限，在执行前判断是否放行或需要用户审批。

    本地工具和 MCP 工具都使用 allow、ask、deny 三种权限：allow 允许调用，
    ask 要求用户批准本次具体操作，deny 禁止调用并从模型工具列表中移除。

    ToolManager 调用 is_exposed 筛选模型可见工具；Pipeline 调用 authorize 判断
    一次调用能否继续，用户作出审批决定后再调用 resolve_authorization 核对原工具
    和参数。设置页的工具目录查询及权限保存由 ToolPermissionService 负责。

    本类返回放行结果、待审批请求或拒绝原因。Pipeline 负责实际执行工具，或保存
    待审批状态并产生中断事件，再由 Scheduler 将审批信息交给界面。

    Attributes:
        _settings_loader (Callable[[str], ChatSettings]): 按会话读取当前配置的函数，权限检查时调用。
    """

    def __init__(
        self,
        settings_loader: Callable[[str], ChatSettings],
    ) -> None:
        """保存配置读取函数，模型请求工具或恢复审批时再读取当前会话权限。

        Args:
            settings_loader (Callable[[str], ChatSettings]): 接收会话编号并返回当前配置，供每次权限检查使用。
        """
        self._settings_loader = settings_loader

    def get_policy(self, session_id: str, identity: ToolIdentity) -> ToolPolicy:
        """按会话配置判断工具是允许、需要审批还是禁止；读取或解析失败时返回禁止。

        MCP 服务未加入该会话或未启用时返回 deny；没有单独设置权限的工具默认 ask。

        Args:
            session_id (str): 本次工具调用所属的会话编号。
            identity (ToolIdentity): 来源、名称及服务编号确定的工具身份。

        Returns:
            ToolPolicy: allow、ask 或 deny；配置读取、解析异常也按 deny 处理。
        """
        try:
            settings = self._settings_loader(session_id)
            if identity.source == ToolSource.MCP:
                server = settings.mcp.servers.get(identity.server_id or "")
                if server is None or not server.enabled:
                    return ToolPolicy.DENY
            policy = settings.tool_permissions.get(identity.stable_key)
            # 1. 内存设置和测试注入也遵循权限改名规则，不依赖 YAML 已经完成补齐。
            if policy is None and identity.source == ToolSource.LOCAL:
                legacy = settings.tool_permissions.get("local:run_ps")
                if identity.name == "exec_command" and legacy is not None:
                    policy = legacy
                elif identity.name == "write_stdin" and legacy == ToolPolicy.DENY:
                    policy = ToolPolicy.DENY
            return ToolPolicy(policy if policy is not None else ToolPolicy.ASK)
        except Exception:
            return ToolPolicy.DENY

    def is_exposed(self, session_id: str, tool: Any) -> bool:
        """供 ToolManager 筛选模型工具列表，供 MCP 适配器在远端调用前复核是否被禁用。

        allow 和 ask 都允许模型看见工具；ask 的具体操作仍由 Pipeline 请求用户审批。

        Args:
            session_id (str): 当前操作所属的会话编号。
            tool (Any): 需要注册或执行的工具对象。

        Returns:
            bool: 当前权限不是 deny 时为 True。
        """
        return self.get_policy(session_id, descriptor_for_tool(tool).identity) != ToolPolicy.DENY

    def get_bound_mcp_identity(self, session_id: str, server_id: str) -> str | None:
        """供 MCP 适配器读取会话中保存的服务身份摘要，与当前连接的服务比较。

        Args:
            session_id (str): 当前操作所属的会话编号。
            server_id (str): MCP 服务的稳定编号。

        Returns:
            str | None: 会话配置记录的服务身份摘要；服务未启用、未绑定或配置读取失败时为空。
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
        """检查一份批准记录是否仍对应当前工具调用，同时遵守用户现在保存的权限。

        核对工具调用编号、工具来源、参数、工具定义及准备好的动作。未携带批准记录时，
        仅 allow 权限或 after_prepare 工具的首次只读准备阶段可以通过。

        Args:
            invocation (ToolInvocation): 当前会话、本次调用编号、工具信息、参数及准备好的动作。
            grant (dict[str, Any] | None): 用户批准本次具体操作后生成的记录；自动允许或只读准备时可为空。

        Returns:
            bool: 本次调用可以通过权限检查时为 True；携带批准记录时还必须与具体操作一致。
        """
        # 1. 先读取当前权限，用户已改为 deny 时，旧批准记录也不能放行。
        policy = self.get_policy(invocation.session_id, invocation.descriptor.identity)
        if policy == ToolPolicy.DENY:
            return False
        # 2. 没有批准记录时，只允许自动放行或尚未形成具体动作的只读准备。
        if grant is None:
            return (
                policy == ToolPolicy.ALLOW
                or (
                    invocation.descriptor.authorization_timing == AuthorizationTiming.AFTER_PREPARE
                    and invocation.prepared_action is None
                )
            )
        # 3. 有批准记录时逐项比较，防止更换工具、参数或屏幕动作后沿用原批准。
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
        """供 Pipeline 在执行工具前判断：直接放行、请求用户审批，或返回拒绝原因。

        after_prepare 工具首次调用时仅放行只读准备，例如截图并定位屏幕目标；工具
        返回具体动作后，Pipeline 再调用本方法，对准备好的操作执行 allow/ask/deny 判断。
        需要审批时只构造请求和待保存材料，由 Pipeline 保存状态并向 Scheduler 产生中断事件。

        Args:
            invocation (ToolInvocation): 当前会话的一次工具调用，以及已准备好的动作（若有）。

        Returns:
            AuthorizationOutcome: Granted 允许 Pipeline 继续；AuthorizationRequired 要求保存状态并等待用户审批；ToolResult 包含禁止使用的原因。
        """
        # 1. 用户禁止的工具立即返回错误，不执行它的准备或实际操作。
        policy = self.get_policy(invocation.session_id, invocation.descriptor.identity)
        if policy == ToolPolicy.DENY:
            return ToolResult(content="错误: 当前工具不允许使用")

        # 2. 需要先准备具体动作的工具，此处仅允许只读准备；返回动作后再次检查权限。
        if (
            invocation.descriptor.authorization_timing == AuthorizationTiming.AFTER_PREPARE
            and invocation.prepared_action is None
        ):
            return Granted()

        # 3. allow 直接放行；ask 则保存本次工具、参数和动作摘要，供审批恢复时比较。
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
        """用户作出审批决定后，由 Pipeline 核对原操作并判断能否继续执行。

        Pipeline 提供已保存的调用信息和本次决定。本方法先确认工具、参数和具体动作
        与发起审批时一致；用户拒绝或权限已变为 deny 时返回原因，批准时生成仅适用于
        本次操作的记录，实际工具执行仍由 Pipeline 负责。

        Args:
            invocation (ToolInvocation): 准备恢复执行的实际工具、参数及具体动作。
            decision (dict[str, Any]): 用户作出的 approve 或 reject 决定；其他决定按请求已过期处理。
            authorization_snapshot (dict[str, Any]): 发起审批时保存的请求编号、工具调用编号及各项内容摘要。

        Returns:
            AuthorizationOutcome: Granted 携带本次操作的批准记录；ToolResult 包含过期、拒绝或禁止执行的原因。
        """
        # 1. 先比较发起审批时保存的操作，参数、工具定义或具体动作变更后不能沿用批准。
        if not self._matches(invocation, authorization_snapshot):
            return ToolResult(content="错误: 工具审批已过期，请重新发起调用")
        # 2. 用户拒绝时结束本次调用；只有明确 approve 才继续生成批准记录。
        kind = decision.get("kind")
        if kind == ApprovalDecisionKind.REJECT:
            return ToolResult(content="工具调用已被用户拒绝")
        if kind != ApprovalDecisionKind.APPROVE:
            return ToolResult(content="错误: 工具审批已过期，请重新发起调用")

        # 3. 再读当前权限：用户即使批准了弹窗，已经改为 deny 的工具仍不能执行。
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
        """发起审批时记录本次操作的工具调用编号和内容摘要，供恢复时比较。

        Args:
            invocation (ToolInvocation): 本次会话、工具调用编号、参数及可选的具体动作。

        Returns:
            dict[str, Any]: 工具调用编号、工具来源标识及参数、定义、动作摘要；调用方另补审批请求编号。
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
        """恢复执行前重新计算操作摘要，与发起审批时保存的字段完整比较。

        Args:
            invocation (ToolInvocation): 当前准备执行的工具调用编号、工具、参数及具体动作。
            snapshot (dict[str, Any]): 发起审批时保存的调用信息及审批请求编号。

        Returns:
            bool: 原记录仅包含预期字段，且调用编号、工具、参数、定义和动作均一致时为 True；请求编号沿用原记录。
        """
        return snapshot == {
            **cls._snapshot(invocation),
            "request_id": snapshot.get("request_id"),
        }
