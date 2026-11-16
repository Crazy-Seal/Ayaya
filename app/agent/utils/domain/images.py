"""图片保存工具函数。"""

import base64
import logging
import os
from pathlib import Path
import uuid
from datetime import datetime

from app.schemas.message_types import MediaKind
from app.environment import get_images_dir

logger = logging.getLogger(__name__)

IMAGE_CATEGORIES = frozenset({MediaKind.TOOL, MediaKind.MCP, MediaKind.ENVIRONMENT})


def save_image_to_disk(
    image_data: str,
    *,
    category: MediaKind | None = None,
) -> str | None:
    """保存单张图片到磁盘。

    Args:
        image_data (str): 图片数据，可以是完整 data URL 或纯 base64
        category (MediaKind | None): 区分真人附件、截图、本地工具及 MCP 图片的保存子目录。

    Returns:
        str | None: 文件名（相对路径），失败返回 None
    """
    try:
        # 确保目录存在
        if category is not None and category not in IMAGE_CATEGORIES:
            raise ValueError(f"不支持的图片目录类别: {category}")
        images_dir = get_images_dir()
        target_dir = images_dir / category if category else images_dir
        os.makedirs(target_dir, exist_ok=True)

        # 解析 data URL 并提取格式
        ext = "png"  # 默认后缀
        if image_data.startswith("data:image"):
            # 提取 MIME 类型：data:image/jpeg;base64,xxx
            mime_start = 5  # "data:" 之后
            mime_end = image_data.find(";")
            if mime_end > mime_start:
                mime_type = image_data[mime_start:mime_end]  # 图片 MIME 类型，例如 image/jpeg。
                mime_ext = mime_type.split("/")[1] if "/" in mime_type else "png"
                # 标准化后缀名
                if mime_ext == "jpeg":
                    ext = "jpg"
                else:
                    ext = "".join(char for char in mime_ext if char.isalnum()) or "png"

            # 提取 base64 部分
            base64_start = image_data.find(",") + 1
            if base64_start == 0:
                logger.warning("[ImageUtils] 无效的 data URL 格式")
                return None
            base64_data = image_data[base64_start:]
        else:
            base64_data = image_data

        # 解码 base64
        image_bytes = base64.b64decode(base64_data)

        # 生成文件名：日期+时间+UUID+后缀
        now = datetime.now()
        timestamp = now.strftime("%Y-%m-%d_%H-%M-%S")
        unique_id = uuid.uuid4().hex[:8]
        filename = f"{timestamp}_{unique_id}.{ext}"
        filepath = target_dir / filename

        # 保存文件
        with open(filepath, "wb") as f:
            f.write(image_bytes)

        logger.info("[ImageUtils] 图片已保存: %s", filename)
        relative_path = Path(category, filename) if category else Path(filename)
        return relative_path.as_posix()

    except Exception as e:
        logger.warning("[ImageUtils] 图片保存失败: %s", e)
        return None


def save_multiple_images(
    images: list[str],
    *,
    category: MediaKind | None = None,
) -> list[str]:
    """保存多张图片到磁盘。

    Args:
        images (list[str]): 图片数据列表
        category (MediaKind | None): 区分真人附件、截图、本地工具及 MCP 图片的保存子目录。

    Returns:
        list[str]: 文件名列表（相对路径）
    """
    filenames: list[str] = []
    for image_data in images:
        filename = save_image_to_disk(image_data, category=category)
        if not filename:
            images_dir = get_images_dir().resolve()
            for saved in filenames:
                try:
                    path = (images_dir / Path(saved)).resolve()
                    if path.is_relative_to(images_dir):
                        path.unlink(missing_ok=True)
                except OSError as exc:
                    logger.warning("[ImageUtils] 清理未完成图片组失败: %s", exc)
            return []
        filenames.append(filename)
    return filenames
