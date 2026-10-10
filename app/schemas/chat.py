from pydantic import BaseModel, Field


# DTO
class ChatHistoryItem(BaseModel):
    """定义桌面可见历史项，旧项缺少来源时默认 desktop。

    Attributes:
        id (str): SQLite 自增输入序号，在事件中作为 input_sequence。
        role (str): 桌面展示角色，可能为 Human、AI 或 AI_Tool_Calling。
        content (str): Assistant 正文。
        timestamp (str): 事件创建的 UTC 时间。
        images (list[str] | None): 真人附件的图片 data URL 列表。
        origin_channel (str): 输入或本轮回复所属的渠道。
    """
    id: str
    role: str
    content: str
    # 已转换为系统本地时区的时间
    timestamp: str
    # 图片文件路径列表
    images: list[str] | None = Field(default=None, description="List of image file paths")
    origin_channel: str = "desktop"
