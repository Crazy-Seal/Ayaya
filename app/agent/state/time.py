"""消息时间的存储、规范化和展示。"""

from datetime import datetime, timezone


WEEKDAYS = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def system_timezone():
    return datetime.now().astimezone().tzinfo


def normalize_utc(value: datetime | str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=system_timezone())
    return parsed.astimezone(timezone.utc)


def format_local_time(value: datetime | str) -> str:
    local = normalize_utc(value).astimezone()
    return f"{local.strftime('%Y-%m-%d %H:%M:%S %z')} {WEEKDAYS[local.weekday()]}"


def render_timed_text(content: str, timestamp: datetime | str) -> str:
    return f"[发送时间：{format_local_time(timestamp)}] {content}"
