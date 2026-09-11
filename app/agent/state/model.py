"""
Agent 状态定义

"""

from pydantic import BaseModel, ConfigDict, Field
from typing import Any
from datetime import datetime
from uuid import uuid4

from app.agent.state.messages import (
    TOOL_IMAGE_MESSAGE_NAME,
    Message,
    ContentPart,
    ToolCall,
    messages_from_openai_format,
)
from app.agent.state.time import utc_now


class TokenBaseline(BaseModel):
    """一次成功主模型请求留下的真实输入 token 锚点。"""

    accounting_key: str
    estimator_version: int
    static_fingerprint: str
    rough_prompt_tokens: int = Field(gt=0)
    actual_prompt_tokens: int = Field(gt=0)


class AgentState(BaseModel):
    """Agent 状态"""

    # ==================== 核心字段 ====================
    session_id: str
    messages: list[dict] = Field(default_factory=list)  # 序列化的消息列表（便于持久化）
    token_baseline: TokenBaseline | None = None

    # ==================== 记忆相关 ====================
    long_term_memory_turn_counter: int = 0

    # ==================== 工具执行上下文 ====================
    pending_tool_calls: list[dict] = Field(default_factory=list)  # 待执行的工具调用
    tool_results: list[dict] = Field(default_factory=list)        # 工具执行结果
    deferred_tool_images: list[dict] = Field(default_factory=list)  # 当前工具批次延迟注入的图片

    # ==================== 中断恢复 ====================
    interrupt_data: dict | None = None              # 中断数据
    pending_actions: list[dict] = Field(default_factory=list)  # 待执行的后续工具

    # ==================== 扩展字段 ====================
    extra: dict[str, Any] = Field(default_factory=dict)  # 供插件使用

    # ==================== 时间戳 ====================
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    model_config = ConfigDict(arbitrary_types_allowed=True)

    # ==================== 消息操作 ====================

    def add_message(self, message: Message) -> None:
        """添加消息"""
        serialized = message.to_openai_format()
        serialized["_message_id"] = uuid4().hex
        serialized["_created_at"] = message.timestamp.isoformat()
        self.messages.append(serialized)
        self.updated_at = utc_now()

    def add_user_message(self, content: str | list[ContentPart]) -> None:
        """添加用户消息"""
        msg = Message.user_message(content)
        self.add_message(msg)

    def add_assistant_message(self, content: str) -> None:
        """添加助手消息"""
        msg = Message.assistant_message(content)
        self.add_message(msg)

    def add_system_message(self, content: str) -> None:
        """添加系统消息"""
        msg = Message.system_message(content)
        self.add_message(msg)

    def add_tool_message(
        self,
        content: str,
        tool_name: str,
        tool_call_id: str | None = None,
        *,
        compressible: bool = False,
    ) -> None:
        """添加工具返回消息"""
        msg = Message.tool_message(content, tool_name, tool_call_id)
        self.add_message(msg)
        self.messages[-1]["_compressible"] = compressible

    def get_messages(self) -> list[Message]:
        """获取消息对象列表（用于处理）"""
        return messages_from_openai_format(self.messages)

    def get_openai_messages(self) -> list[dict]:
        """获取 OpenAI 格式的消息列表（用于 API 调用）"""
        from app.agent.utils.domain.text import normalize_messages_for_model

        return normalize_messages_for_model(self.messages)

    # ==================== 工具调用操作 ====================

    def add_pending_tool_call(self, tool_call: ToolCall) -> None:
        """添加待执行的工具调用"""
        self.pending_tool_calls.append(tool_call.to_dict())
        self.updated_at = utc_now()

    def set_pending_tool_calls(self, tool_calls: list[ToolCall]) -> None:
        """用一组新的工具调用替换当前待执行列表。"""
        self.pending_tool_calls = [tool_call.to_dict() for tool_call in tool_calls]
        self.updated_at = utc_now()

    def remove_pending_tool_call(self, tool_call_id: str) -> None:
        """按调用 ID 移除一个已经获得结果的工具调用。"""
        self.pending_tool_calls = [
            item for item in self.pending_tool_calls
            if item.get("id") != tool_call_id
        ]
        self.updated_at = utc_now()

    def clear_pending_tool_calls(self) -> None:
        """清空待执行的工具调用"""
        self.pending_tool_calls = []
        self.updated_at = utc_now()

    def get_pending_tool_calls(self) -> list[ToolCall]:
        """获取待执行的工具调用列表"""
        from app.agent.state.messages import ToolCall
        return [ToolCall.from_dict(tc) for tc in self.pending_tool_calls]

    def flush_deferred_tool_images(self) -> None:
        """在整批工具消息补齐后统一注入工具图片。"""
        deferred = list(self.deferred_tool_images)
        self.deferred_tool_images = []
        for item in deferred:
            image_urls = list(item.get("image_urls") or [])
            if not image_urls:
                continue
            tool_name = str(item.get("tool_name") or "")
            associated_message_id = item.get("associated_message_id")
            if not associated_message_id:
                tool_call_id = item.get("tool_call_id")
                if tool_call_id:
                    source = next(
                        (
                            message for message in reversed(self.messages)
                            if message.get("role") == "tool"
                            and message.get("tool_call_id") == tool_call_id
                        ),
                        None,
                    )
                    associated_message_id = source.get("_message_id") if source else None
            self.add_message(Message.user_message(
                [
                    ContentPart.text_part(f"[系统消息]工具 {tool_name} 返回图片: "),
                    *(ContentPart.image_part(url) for url in image_urls),
                ],
                name=str(item.get("image_message_name") or TOOL_IMAGE_MESSAGE_NAME),
            ))
            if associated_message_id:
                self.messages[-1]["_associated_message_id"] = str(associated_message_id)

    # ==================== 中断恢复操作 ====================

    def set_interrupt(self, interrupt_data: dict, pending_actions: list[dict] | None = None) -> None:
        """设置中断状态"""
        self.interrupt_data = interrupt_data
        self.pending_actions = pending_actions or []
        self.updated_at = utc_now()

    def clear_interrupt(self) -> None:
        """清除中断状态"""
        self.interrupt_data = None
        self.pending_actions = []
        self.updated_at = utc_now()

    def is_interrupted(self) -> bool:
        """检查是否处于中断状态"""
        return self.interrupt_data is not None

    def increment_long_term_memory_turn_counter(self) -> int:
        """记录一条已接收的真实用户消息，返回尚未处理的消息数。"""
        self.long_term_memory_turn_counter += 1
        self.updated_at = utc_now()
        return self.long_term_memory_turn_counter

    def reset_long_term_memory_turn_counter(self) -> None:
        """标记当前累计的真实用户消息已经进入长期记忆批次。"""
        self.long_term_memory_turn_counter = 0
        self.updated_at = utc_now()

    # ==================== 扩展字段操作 ====================

    def set_extra(self, key: str, value: Any) -> None:
        """设置扩展字段"""
        self.extra[key] = value
        self.updated_at = utc_now()

    def get_extra(self, key: str, default: Any = None) -> Any:
        """获取扩展字段"""
        return self.extra.get(key, default)

    def update_extra(self, data: dict) -> None:
        """批量更新扩展字段"""
        self.extra.update(data)
        self.updated_at = utc_now()

    # ==================== 序列化 ====================

    def to_checkpoint(self) -> dict:
        """序列化为 checkpoint"""
        return self.model_dump()

    @classmethod
    def from_checkpoint(cls, data: dict) -> "AgentState":
        """从 checkpoint 恢复"""
        return cls(**data)

    @classmethod
    def create_new(cls, session_id: str) -> "AgentState":
        """创建新的状态"""
        return cls(session_id=session_id)
