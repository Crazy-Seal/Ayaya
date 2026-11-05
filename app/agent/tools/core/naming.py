"""为模型公开名称生成稳定命名空间，避免不同能力互相覆盖。"""

import re


def exposed_tool_name(*parts: str) -> str:
    """按命名空间拼接公开名称，名称不合法时在装配阶段报告错误。

    Args:
        *parts (str): 来源、插件、服务及原始工具名称。

    Returns:
        str: 使用双下划线连接各部分、最长 64 位的公开名称。

    Raises:
        ValueError: 拼接后的名称为空、超过长度限制或包含不支持的字符。
    """
    name = "__".join(parts)
    if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name) is None:
        raise ValueError(f"工具公开名称只能包含字母、数字、下划线和连字符，长度为 1 至 64 位：{name}")
    return name
