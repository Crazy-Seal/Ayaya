"""图片保存工具函数。"""

import base64
import logging
import mimetypes
import os
from functools import lru_cache
from pathlib import Path
import uuid
from datetime import datetime

from app.schemas.message_types import MediaKind
from app.environment import get_images_dir

logger = logging.getLogger(__name__)

IMAGE_CATEGORIES = frozenset({MediaKind.TOOL, MediaKind.MCP, MediaKind.ENVIRONMENT})
IMAGE_CACHE_SIZE = 32


def resolve_image_path(storage_ref: str, *, images_dir: Path | None = None) -> Path:
    """将消息中的图片引用解析为当前数据目录内的文件位置。

    Args:
        storage_ref (str): 图片根目录下的相对路径。
        images_dir (Path | None): 离线迁移使用的图片根目录，省略时使用当前环境。
    Returns:
        Path: 已规范化且位于图片目录内的绝对路径。
    Raises:
        ValueError: 引用为空、包含非法目录或超出图片根目录。
    """
    root = (images_dir if images_dir is not None else get_images_dir()).resolve()
    parts = storage_ref.split("/")
    if (not storage_ref or "\\" in storage_ref or ":" in storage_ref
            or any(part in {"", ".", ".."} for part in parts)
            or len(parts) > 2 or (len(parts) == 2 and parts[0] not in IMAGE_CATEGORIES)):
        raise ValueError("非法图片文件引用")
    path = (root / storage_ref).resolve()
    if not path.is_relative_to(root):
        raise ValueError("图片引用超出存储目录")
    return path


@lru_cache(maxsize=IMAGE_CACHE_SIZE)
def _read_image_data_url(path: Path, signature: tuple[int, int, int, int]) -> str:
    """缓存同一版本文件的模型图片编码。

    Args:
        path (Path): 通过引用校验的图片文件。
        signature (tuple[int, int, int, int]): 文件修改时间、状态变更时间、大小及文件标识。
    Returns:
        str: 模型可接收的 data URL。
    Raises:
        OSError: 文件读取失败。
        ValueError: 文件扩展名没有对应的图片 MIME。
    """
    mime = mimetypes.guess_type(path.name)[0]
    if mime is None or not mime.startswith("image/"):
        raise ValueError("无法识别图片类型")
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"


def load_image_data_url(storage_ref: str) -> str:
    """模型请求前读取已保存图片，文件删除或变更后重新检查缓存。

    Args:
        storage_ref (str): 当前环境图片根目录内的文件引用。
    Returns:
        str: 当前文件对应的 data URL。
    Raises:
        OSError: 文件缺失或不可读。
        ValueError: 引用或图片类型无效。
    """
    path = resolve_image_path(storage_ref)
    stat = path.stat()
    return _read_image_data_url(path, (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino))


def save_image_to_disk(
    image_data: str,
    *,
    category: MediaKind | None = None,
) -> str | None:
    """保存单张图片到磁盘。

    Args:
        image_data (str): 图片数据，可以是完整 data URL 或纯 base64
        category (MediaKind | None): 区分本地工具、MCP 和环境图片，省略时保存为根目录附件。

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
        category (MediaKind | None): 区分本地工具、MCP 和环境图片，省略时保存为根目录附件。

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
