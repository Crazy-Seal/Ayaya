from app.schemas.message_types import HistoryRole
from app.schemas.runtime_types import DESKTOP_CHANNEL
from pydantic import BaseModel, Field


# 路由将数据库协议记录转换成这些字段，供聊天列表恢复可见历史。
class ChatHistoryItem(BaseModel):
    """定义桌面可见历史项，旧项缺少来源时默认 desktop。

    Attributes:
        id (str): 用于聊天历史去重的稳定消息编号；工具提示可在原消息编号后追加 :tools。
        role (HistoryRole): 桌面展示角色，可能为 Human、AI 或 AI_Tool_Calling。
        content (str): 当前可见项的真人正文、助手正文或工具调用提示。
        timestamp (str): 协议记录转换为本机时区后的显示时间。
        images (list[str] | None): 图片文件路径，未成功外置时可保留原图片地址。
        origin_channel (str): 输入或本轮回复所属的渠道。
    """
    id: str
    role: HistoryRole
    content: str
    # 已转换为系统本地时区的时间
    timestamp: str
    # 图片文件路径列表
    images: list[str] | None = Field(default=None, description="List of image file paths")
    origin_channel: str = DESKTOP_CHANNEL
