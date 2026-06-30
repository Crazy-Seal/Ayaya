"""图片处理工具函数和缓存管理"""

import asyncio
import base64
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime

from app.runtime import get_images_dir

logger = logging.getLogger(__name__)

# ==================== 数据类 ====================

@dataclass
class ImageTaskResult:
    """图片描述任务结果"""
    description: str       # 图片描述（单字符串）
    filenames: list[str]   # 文件名列表


# ==================== 模块级缓存 ====================

# 后台任务缓存：存储正在执行的图片描述生成任务
_image_description_tasks: dict[str, asyncio.Task] = {}


# ==================== 缓存管理函数 ====================

def get_cache_key(session_id: str, message_id: str) -> str:
    """生成缓存 key。

    Args:
        session_id: 会话 ID
        message_id: 消息 ID

    Returns:
        缓存 key，格式为 "{session_id}:{message_id}"
    """
    return f"{session_id}:{message_id}"


def set_image_task(key: str, task: asyncio.Task) -> None:
    """存储后台任务。

    Args:
        key: 缓存 key
        task: asyncio.Task 对象
    """
    _image_description_tasks[key] = task


def get_image_task(key: str) -> asyncio.Task | None:
    """获取后台任务。

    Args:
        key: 缓存 key

    Returns:
        asyncio.Task 对象，不存在则返回 None
    """
    return _image_description_tasks.get(key)


def clear_task(key: str) -> None:
    """清理任务缓存。

    Args:
        key: 缓存 key
    """
    _image_description_tasks.pop(key, None)


def cancel_task(key: str) -> None:
    """取消并移除后续不会再被消费的图片任务。"""
    task = _image_description_tasks.pop(key, None)
    if task is not None and not task.done():
        task.cancel()


# ==================== 图片保存函数 ====================

def save_image_to_disk(image_data: str) -> str | None:
    """保存单张图片到磁盘。

    Args:
        image_data: 图片数据，可以是完整 data URL 或纯 base64

    Returns:
        文件名（相对路径），失败返回 None
    """
    try:
        # 确保目录存在
        images_dir = get_images_dir()
        os.makedirs(images_dir, exist_ok=True)

        # 解析 data URL 并提取格式
        ext = "png"  # 默认后缀
        if image_data.startswith("data:image"):
            # 提取 MIME 类型：data:image/jpeg;base64,xxx
            mime_start = 5  # "data:" 之后
            mime_end = image_data.find(";")
            if mime_end > mime_start:
                mime_type = image_data[mime_start:mime_end]  # "image/jpeg"
                mime_ext = mime_type.split("/")[1] if "/" in mime_type else "png"
                # 标准化后缀名
                if mime_ext == "jpeg":
                    ext = "jpg"
                else:
                    ext = mime_ext

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
        filepath = images_dir / filename

        # 保存文件
        with open(filepath, "wb") as f:
            f.write(image_bytes)

        logger.info("[ImageUtils] 图片已保存: %s", filename)
        return filename

    except Exception as e:
        logger.warning("[ImageUtils] 图片保存失败: %s", e)
        return None


def save_multiple_images(images: list[str]) -> list[str]:
    """保存多张图片到磁盘。

    Args:
        images: 图片数据列表

    Returns:
        文件名列表（相对路径）
    """
    filenames: list[str] = []
    for image_data in images:
        filename = save_image_to_disk(image_data)
        if filename:
            filenames.append(filename)
    return filenames


# ==================== 消息内容处理函数 ====================

def has_image_content(content: object) -> bool:
    """检查消息内容是否包含图片。

    Args:
        content: 消息内容，可能是字符串或列表

    Returns:
        是否包含图片
    """
    if isinstance(content, str):
        return False

    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "image_url":
                return True

    return False
