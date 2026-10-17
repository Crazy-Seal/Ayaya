"""内置工具与 MCP 工具共享的结果资源限制。"""

MAX_IMAGES_PER_CALL = 5
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_TOTAL_IMAGE_BYTES = 25 * 1024 * 1024
ALLOWED_IMAGE_MIME_TYPES = frozenset({"image/png", "image/jpeg", "image/webp"})


__all__ = [
    "ALLOWED_IMAGE_MIME_TYPES",
    "MAX_IMAGE_BYTES",
    "MAX_IMAGES_PER_CALL",
    "MAX_TOTAL_IMAGE_BYTES",
]
