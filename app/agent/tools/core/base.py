"""工具执行契约、结果类型与基础实现。"""


import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from app.schemas.tool_types import AuthorizationTiming
from app.schemas.message_types import MediaKind

from app.schemas.runtime_types import (
    InterruptKind,
)
from app.agent.state.model import AgentState
from app.agent.state.messages import ContentPart, ImageContent, text_content, validate_content, content_text
from app.agent.models.contracts import ToolDefinition

if TYPE_CHECKING:
    from app.agent.tools.core.runtime import ToolRuntime


# ==================== 中断事件 ====================

@dataclass
class InterruptEvent:
    """中断事件 - 用于需要用户确认的场景

    区分两类序列化：
    - to_client(): 提供交互事件与桌面执行通道使用的最小展示字段
    - to_state():  写入 AgentState.interrupt_data 的完整快照，
                   含恢复同一工具所需的全部路由信息（可持久化、跨请求）
    """
    type: InterruptKind | str    # 已知中断或沿既有路径处理的扩展类型。
    request_id: str              # 请求唯一标识
    message: str                 # 展示给用户的提示
    data: dict = field(default_factory=dict)  # 额外数据（发给前端的附加内容）

    # 恢复路由（持久化，不一定发给前端）
    tool_name: str = ""          # 被中断的工具名
    tool_call_id: str = ""       # 被中断的工具调用 id
    tool_args: dict = field(default_factory=dict)  # 工具原始参数
    resume_state: dict = field(default_factory=dict)  # 工具中间态（如外部操作回执）

    def __post_init__(self) -> None:
        """已知中断归一化为枚举，扩展值继续沿既有工具恢复路径处理。"""
        try:
            self.type = InterruptKind(self.type)
        except (ValueError, TypeError):
            pass

    @classmethod
    def create(cls, type: InterruptKind | str, message: str, data: dict | None = None) -> "InterruptEvent":
        """创建带稳定请求身份的中断事件，不执行工具副作用。

        Args:
            type (InterruptKind | str): 已知中断类型或既有扩展值。
            message (str): 交互端展示文案。
            data (dict | None): 需要随请求发送的数据。

        Returns:
            InterruptEvent: 可展示与持久恢复的中断对象。
        """
        return cls(
            type=type,
            request_id=str(uuid.uuid4()),
            message=message,
            data=data or {}
        )

    def to_client(self) -> dict:
        """发给前端的最小字段（SSE value）。"""
        payload = {
            "type": self.type,
            "request_id": self.request_id,
            "message": self.message,
        }
        if self.data:
            payload["data"] = self.data
        return payload

    def to_state(self) -> dict:
        """写入 AgentState.interrupt_data 的完整持久化快照"""
        return {
            "type": self.type,
            "request_id": self.request_id,
            "message": self.message,
            "data": self.data,
            "tool_name": self.tool_name,
            "tool_call_id": self.tool_call_id,
            "tool_args": self.tool_args,
            "resume_state": self.resume_state,
        }

    # 默认序列化为客户端字段，避免持久化路由信息进入 SSE。
    def to_dict(self) -> dict:
        """提供现有调用方使用的中断字典接口，沿用界面安全字段筛选。

        Returns:
            dict: 与 to_client 相同的界面中断载荷，不含私有恢复材料。
        """
        return self.to_client()


# ==================== 工具相关 ====================

@dataclass
class ToolContext:
    """向单次工具提供状态、授权快照、恢复数据与后台任务启动端口。

    Attributes:
        session_id (str): 共享 Ayaya 会话标识。
        state (AgentState): 当前 Agent 状态。
        resume_data (dict | None): 当前工具的人工审批决定。
        resume_state (dict | None): 中断前保存的工具中间状态。
        authorization_grant (dict | None): 绑定当前调用的单次授权快照。
        runtime (ToolRuntime | None): 当前工具调用绑定的通用任务能力。
        origin_run_id (str): 发起进程任务的运行轮次标识。
        origin_tool_call_id (str): 发起任务的工具调用标识。
        origin_tool_name (str): Pipeline 注入的实际调用工具名。
    """
    session_id: str
    state: AgentState
    resume_data: dict | None = None                     # 中断恢复时用户回传的数据
    resume_state: dict | None = None                    # 中断前工具暂存的中间态（恢复执行时读取）
    authorization_grant: dict | None = None             # 当前调用绑定的单次授权快照
    runtime: "ToolRuntime | None" = None
    origin_run_id: str = ""
    origin_tool_call_id: str = ""
    origin_tool_name: str = ""


@dataclass
class ToolResult:
    """承载工具正文、图片、状态更新，以及需要等待审批或桌面的继续执行材料。"""
    content: tuple[ContentPart, ...]
    interrupt: InterruptEvent | None = None
    state_updates: dict = field(default_factory=dict)
    resume_state: dict = field(default_factory=dict)
    prepared_action: dict | None = None

    def __post_init__(self) -> None:
        """固定完整内容；纯文本构造是便捷输入，不是第二种存储格式。"""
        self.content = text_content(self.content) if isinstance(self.content, str) else validate_content(self.content)

    @property
    def text(self) -> str:
        """供工具错误判断和日志读取纯文字。"""
        return content_text(self.content)

    @classmethod
    def success(cls, content: str, state_updates: dict | None = None,
                image_url: str | None = None, image_urls: list[str] | None = None,
                media_kind: MediaKind = MediaKind.TOOL) -> "ToolResult":
        """把内置工具的文字与图片立即组成完整业务结果。

        Args:
            content (str): 工具文字。
            state_updates (dict | None): 随结果交给状态的更新。
            image_url (str | None): 一张工具图片。
            image_urls (list[str] | None): 多张工具图片。
            media_kind (MediaKind): tool 或 mcp。
        Returns:
            ToolResult: 完整有序内容，尚未规范化或保存。
        """
        urls = list(image_urls or [])
        if image_url and image_url not in urls:
            urls.insert(0, image_url)
        return cls(content=(*text_content(content), *(ImageContent(url, media_kind=media_kind) for url in urls)),
                   state_updates=state_updates or {})

    @classmethod
    def interrupt_result(cls, interrupt: InterruptEvent) -> "ToolResult":
        """创建等待用户或运行时处理的工具结果，携带中断信息和恢复状态。

        Args:
            interrupt (InterruptEvent): 当前工具要求等待审批或桌面处理的中断事件。
        """
        return cls(content="", interrupt=interrupt)

    @classmethod
    def needs_input(
        cls,
        type: InterruptKind | str,
        message: str,
        resume_state: dict | None = None,
        data: dict | None = None,
    ) -> "ToolResult":
        """创建「需要用户输入」的中断结果（可恢复工具在首次执行、尚未拿到用户输入时使用）

        Args:
            type (InterruptKind | str): 已知中断类型或沿原路径处理的扩展值。
            message (str): 展示给用户的提示。
            resume_state (dict | None): 恢复执行时需要的中间态。
            data (dict | None): 发给前端的附加数据。

        Returns:
            ToolResult: 不执行副作用的中断结果。
        """
        return cls(
            content="",
            interrupt=InterruptEvent.create(type=type, message=message, data=data),
            resume_state=resume_state or {},
        )


    @classmethod
    def prepared(
        cls,
        prepared_action: dict,
        *,
        resume_state: dict | None = None,
    ) -> "ToolResult":
        """返回 after_prepare 工具已经解析完成、尚待授权的动作。

        Args:
            prepared_action (dict): 只读准备完成后的具体动作，执行前仍需授权。
            resume_state (dict | None): 工具继续执行所需的私有恢复材料。
        """
        return cls(
            content="",
            prepared_action=prepared_action,
            resume_state=resume_state or {},
        )


# ==================== 工具基类 ====================

class BaseTool(ABC):
    """约定模型工具定义与执行接口，具体工具由执行管道注入上下文后调用。"""

    name: str                    # 工具名称
    description: str             # 工具描述（给 LLM 看）
    parameters_schema: dict      # JSON Schema 格式的参数定义
    compressible: bool = False   # 旧结果是否允许从活跃模型上下文回收，不代表允许重放工具
    persist_oversized_result: bool = True  # 超长结果是否允许保存为 tool-result artifact
    authorization_timing: AuthorizationTiming = AuthorizationTiming.BEFORE_EXECUTE

    @abstractmethod
    async def execute(
        self,
        args: dict,
        context: ToolContext
    ) -> ToolResult:
        """由执行管道调用的工具正文接口，具体子类负责执行本工具并返回结果。

        Args:
            args (dict): 当前工具调用或被包装函数的参数。
            context (ToolContext): 执行管道提供的工具运行上下文。
        """
        pass

    def definition(self) -> ToolDefinition:
        """返回供应商无关的公开契约，参数 Schema 在适配器内转换。"""
        return ToolDefinition(self.name, self.description, self.parameters_schema)
