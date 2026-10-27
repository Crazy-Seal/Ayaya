"""直接捕获主屏幕并编码环境图片，保留桌宠和聊天窗口的当前显隐状态。"""

import base64
from io import BytesIO

from PIL import Image, ImageGrab

from app.agent.state import ImageContent
from app.schemas.message_types import MediaKind


MAX_CAPTURE_SIZE = (1920, 1080)


def capture_desktop() -> ImageContent:
    """在工作线程中捕获主屏幕，等比例缩小并返回仅供模型请求使用的 PNG。

    Returns:
        ImageContent: 标记为 ENVIRONMENT 的内存图片，保留透明应用窗口。

    Raises:
        OSError: 系统无法捕获桌面或图像无法编码。
    """
    # 1. 直接获取当前画面，包含透明窗口，不改变任何窗口的显隐状态。
    with ImageGrab.grab(include_layered_windows=True, all_screens=False) as image, BytesIO() as output:
        # 2. 限制模型输入图片尺寸，PNG 数据只保存在本次返回值中。
        image.thumbnail(MAX_CAPTURE_SIZE, Image.Resampling.LANCZOS)
        image.save(output, format="PNG")
        data = base64.b64encode(output.getvalue()).decode("ascii")
    return ImageContent(url=f"data:image/png;base64,{data}", media_kind=MediaKind.ENVIRONMENT)
