"""消息时间的存储、规范化和展示。"""

from datetime import datetime, timezone


WEEKDAYS = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")


def utc_now() -> datetime:
    """为消息和状态记录提供带时区的当前 UTC 时间。

    Returns:
        datetime: 带 UTC 时区的当前时间。
    """
    return datetime.now(timezone.utc)


def system_timezone():
    """读取本机当前时区，供无时区的旧时间记录按本地时间解释。

    Returns:
        tzinfo | None: 系统当前使用的时区对象。
    """
    return datetime.now().astimezone().tzinfo


def normalize_utc(value: datetime | str) -> datetime:
    """将时间字符串或 datetime 统一为 UTC，无时区值先按本机时区解释。

    Args:
        value (datetime | str): ISO 时间字符串或 datetime，可包含 Z 或时区偏移。

    Returns:
        datetime: 带 UTC 时区的时间对象。

    Raises:
        ValueError: 字符串不是可解析的 ISO 时间。
    """
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=system_timezone())
    return parsed.astimezone(timezone.utc)


def format_local_time(value: datetime | str) -> str:
    """将保存的 UTC 时间转成本机时间，并补上偏移和星期用于模型阅读。

    Args:
        value (datetime | str): 当前需要校验或转换的值。

    Returns:
        str: 包含日期、时间、时区偏移及中文星期的文本。
    """
    local = normalize_utc(value).astimezone()
    return f"{local.strftime('%Y-%m-%d %H:%M:%S %z')} {WEEKDAYS[local.weekday()]}"


def render_timed_text(content: str, timestamp: datetime | str) -> str:
    """构造临时模型正文的发送时间前缀，不修改原消息正文。

    Args:
        content (str): 当前处理的正文。
        timestamp (datetime | str): 待转换或展示的时间。

    Returns:
        str: 发送时间标签与原文本组成的字符串。
    """
    return f"[发送时间：{format_local_time(timestamp)}] {content}"
