"""为模型公开名称生成稳定命名空间，避免不同能力互相覆盖。"""

import hashlib
import re
import json
from app.schemas.tool_types import PLUGIN_TOOL_NAMESPACE


def exposed_tool_name(*parts: str) -> str:
    """拼接名称并处理模型允许的字符和长度。

    Args:
        *parts (str): 来源、插件、服务及原始工具名称。

    Returns:
        str: 最长 64 位、包含必要摘要的稳定名称。
    """
    raw = "__".join(parts)
    normalized = re.sub(r"[^A-Za-z0-9_-]", "_", raw)
    structured = parts[0] == PLUGIN_TOOL_NAMESPACE or any("__" in part for part in parts)
    if normalized == raw and len(normalized) <= 64 and not structured:
        return normalized
    material = json.dumps(parts, ensure_ascii=False) if structured else raw
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:8]
    return f"{normalized[:54]}__{digest}"[:64]
