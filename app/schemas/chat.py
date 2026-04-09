from pydantic import BaseModel, Field

# VO
class ChatRequest(BaseModel):
    # 用户输入文本
    message: str = Field(..., min_length=1, description="User input message")
    # 会话标识：同一个 session_id 可共享上下文
    session_id: str = Field(default="default", description="Conversation session id")
    # 图像信息（HTTP 请求中以 base64 字符串形式传递）
    image_data: str | None = Field(default=None, description="Image data from HTTP request")
    # 文档文件名（预留接口）
    document_name: str | None = Field(default=None, description="Document filename")

# VO
class ChatResponse(BaseModel):
    # Agent 返回文本
    response: str
    # 本次使用的模型名（用于调试和观测）
    model: str

# Agent 输入数据结构，包含用户文本输入、图像信息和文档信息（预留接口）
class AgentInput(BaseModel):
    # 用户输入文本
    message: str = Field(..., min_length=1, description="User input message")
    # 图像信息
    image_data: str | None = Field(default=None, description="Base64 encoded image data")
    # 文档文件名（预留接口）
    document_name: str | None = Field(default=None, description="Document filename")

# DTO
class ChatHistoryItem(BaseModel):
    role: str
    content: str
    # 已转换为系统本地时区的时间
    timestamp: str
