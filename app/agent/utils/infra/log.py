import datetime as dt
import logging
from collections.abc import Mapping
from pathlib import Path
from threading import Lock
from typing import TypeAlias, TypeVar

from app.environment import get_data_dir, is_test_environment


_ResultT = TypeVar("_ResultT")
_LogValue: TypeAlias = str | int | float | bool | None | list["_LogValue"] | dict[str, "_LogValue"]


def shorten_for_log(text: str, max_len: int = 200) -> str:
    """限制日志长度，避免控制台被超长内容刷屏。

    Args:
        text (str): 当前处理的文本。
        max_len (int): 日志预览最多保留的字符数。
    """
    if len(text) <= max_len:
        return text
    return f"{text[:max_len]}..."


_SENSITIVE_KEYS = {"api_key", "token", "password", "secret", "authorization"}
_TOOL_LOGGER_LOCK = Lock()
# 生产环境继续使用原有工具日志位置。
_PRODUCTION_TOOL_LOG_FILE = Path(__file__).resolve().parents[2] / "tools" / "tools.log"


def _get_tool_log_file() -> Path:
    """按运行环境返回工具日志路径，避免测试日志污染生产日志。"""
    if is_test_environment():
        return get_data_dir() / "logs" / "tools.log"
    return _PRODUCTION_TOOL_LOG_FILE


def _ensure_tool_file_handler(logger: logging.Logger) -> None:
    """为工具日志器附加文件输出，避免重复添加 handler。

    Args:
        logger (logging.Logger): 记录当前工具调用的日志对象。
    """
    tool_log_file = _get_tool_log_file()
    target = str(tool_log_file.resolve())
    with _TOOL_LOGGER_LOCK:
        for handler in logger.handlers:
            if isinstance(handler, logging.FileHandler) and getattr(handler, "baseFilename", "") == target:
                return

        tool_log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(tool_log_file, encoding="utf-8")
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
        )
        logger.addHandler(file_handler)


def _sanitize_value(value: object) -> _LogValue:
    """递归读取普通字典和只读映射，将参数及输出转换为脱敏、限长的日志片段。

    Args:
        value (object): 工具调用的任意参数或结果，按实际类型转换为日志内容。

    Returns:
        _LogValue: 日志使用的容器副本或标量，原工具参数保持不变。
    """
    if isinstance(value, str):
        return shorten_for_log(value, max_len=500)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, Mapping):
        safe: dict[str, _LogValue] = {}
        for key, item in value.items():
            key_str = str(key)
            if any(word in key_str.lower() for word in _SENSITIVE_KEYS):
                safe[key_str] = "***"
            else:
                safe[key_str] = _sanitize_value(item)
        return safe
    if isinstance(value, (list, tuple)):
        return [_sanitize_value(item) for item in value[:20]]
    return shorten_for_log(repr(value), max_len=500)


def _sanitize_params(params: dict[str, object]) -> dict[str, _LogValue]:
    """按参数名遮蔽凭据类字段，再递归清理其他值，供工具调用日志使用。

    Args:
        params (dict[str, object]): 原始调用参数，按名称过滤敏感字段。

    Returns:
        dict[str, _LogValue]: 可写日志的参数副本，敏感字段显示为星号。
    """
    safe: dict[str, _LogValue] = {}
    for key, value in params.items():
        key_lower = key.lower()
        if any(word in key_lower for word in _SENSITIVE_KEYS):
            safe[key] = "***"
            continue
        safe[key] = _sanitize_value(value)
    return safe


async def log_tool_call_result(
    tool_name: str,
    params: Mapping[str, object],
    result: _ResultT,
    *,
    logger_name: str = "app.agent.tools",
) -> _ResultT:
    """记录一次工具调用的参数和输出。

    适用于调用点已取得工具名、普通或只读参数映射和结果的场景，写入前统一脱敏。

    返回原始 result，方便链式书写：return await log_tool_call_result(...)

    Args:
        tool_name (str): 注册表中使用的工具名称。
        params (Mapping[str, object]): 工具实际使用的调用参数，日志会递归转换并脱敏。
        result (_ResultT): 当前工具或被包装函数返回的结果，记录后保留原类型及对象。
        logger_name (str): 当前工具日志使用的 logger 名称。

    Returns:
        _ResultT: 原始结果对象，类型与传入结果一致。
    """
    logger = logging.getLogger(logger_name)
    _ensure_tool_file_handler(logger)
    call_time = dt.datetime.now(dt.UTC).isoformat()
    safe_params = _sanitize_params(dict(params or {}))
    result_preview = _sanitize_value(result)

    # 结果若为字符串且以 "错误:" 开头，按错误记录
    text_preview = result if isinstance(result, str) else getattr(result, "content", None)
    if isinstance(text_preview, str) and text_preview.lstrip().startswith("错误:"):
        logger.error(
            "tool=%s event=error call_time=%s params=%s error=%s",
            tool_name, call_time, safe_params, result_preview,
        )
    else:
        logger.info(
            "tool=%s event=success call_time=%s params=%s output=%s",
            tool_name, call_time, safe_params, result_preview,
        )
    return result
