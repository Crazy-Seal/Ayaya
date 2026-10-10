import datetime as dt
import functools
import inspect
import logging
import time
from pathlib import Path
from threading import Lock
from typing import Any

from app.runtime import get_data_dir, is_test_environment


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


def _sanitize_value(value: Any) -> Any:
    """将参数/输出转换为可读且安全的日志片段。

    Args:
        value (Any): 当前需要校验或转换的值。
    """
    if isinstance(value, str):
        return shorten_for_log(value, max_len=500)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        safe: dict[str, Any] = {}
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


def _sanitize_params(params: dict[str, Any]) -> dict[str, Any]:
    """按参数名遮蔽凭据类字段，再递归清理其他值，供工具调用日志使用。

    Args:
        params (dict[str, Any]): 经过敏感字段过滤的调用参数。

    Returns:
        dict[str, Any]: 可写日志的参数副本，敏感字段显示为星号。
    """
    safe: dict[str, Any] = {}
    for key, value in params.items():
        key_lower = key.lower()
        if any(word in key_lower for word in _SENSITIVE_KEYS):
            safe[key] = "***"
            continue
        safe[key] = _sanitize_value(value)
    return safe


async def log_tool_call_result(
    tool_name: str,
    params: dict[str, Any],
    result: Any,
    *,
    logger_name: str = "app.agent.tools",
) -> Any:
    """记录一次工具调用的参数和输出。

    适用于调用点已取得工具名、参数字典和结果的场景，写入前统一脱敏。

    返回原始 result，方便链式书写：return await log_tool_call_result(...)

    Args:
        tool_name (str): 注册表中使用的工具名称。
        params (dict[str, Any]): 经过敏感字段过滤的调用参数。
        result (Any): 当前工具或被包装函数返回的结果。
        logger_name (str): 当前工具日志使用的 logger 名称。
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


def log_tool_call(logger_name: str = "app.agent.tools"):
    """为普通同步或异步函数记录调用时间、参数、输出和异常。

    Args:
        logger_name (str): 当前工具日志使用的 logger 名称。
    """

    def decorator(func):
        """绑定原工具函数签名，按同步或异步类型返回带日志的包装函数。

        Args:
            func (Any): 需要包装或检查签名的函数。

        Returns:
            Callable: 保持原返回结果及异常行为的工具包装函数。
        """
        # 1. 装饰时只读取签名；真实执行前再绑定参数、遮蔽敏感值并记录开始时间。
        signature = inspect.signature(func)

        def _prepare_log_context(args, kwargs):
            """包装函数执行前绑定并过滤参数，记录开始时间并确保工具文件日志已初始化。

            Args:
                args (Any): 当前工具调用或被包装函数的参数。
                kwargs (Any): 传递给被调用函数的其他关键字参数。

            Returns:
                tuple[Logger, str, float, dict]: 日志对象、调用时间、计时起点和过滤后的参数。
            """
            logger = logging.getLogger(logger_name)
            _ensure_tool_file_handler(logger)
            call_time = dt.datetime.now(dt.UTC).isoformat()
            start = time.perf_counter()
            try:
                bound = signature.bind_partial(*args, **kwargs)
                params = _sanitize_params(dict(bound.arguments))
            except Exception:
                params = {"args": _sanitize_value(args), "kwargs": _sanitize_value(kwargs)}
            logger.info("tool=%s event=start call_time=%s params=%s", func.__name__, call_time, params)
            return logger, call_time, start, params

        def _log_success_or_error(logger, call_time, start, params, result):
            """原函数正常返回后记录耗时，返回文本以错误前缀开头时使用错误级别。

            Args:
                logger (Any): 记录当前工具调用的日志对象。
                call_time (Any): 当前工具开始执行时的 ISO 时间。
                start (Any): 工具开始执行时的 perf_counter 值。
                params (Any): 经过敏感字段过滤的调用参数。
                result (Any): 当前工具或被包装函数返回的结果。
            """
            duration_ms = int((time.perf_counter() - start) * 1000)
            result_preview = _sanitize_value(result)
            if isinstance(result, str) and result.lstrip().startswith("错误:"):
                logger.error(
                    "tool=%s event=error call_time=%s duration_ms=%s params=%s error=%s",
                    func.__name__,
                    call_time,
                    duration_ms,
                    params,
                    result_preview,
                )
            else:
                logger.info(
                    "tool=%s event=success call_time=%s duration_ms=%s params=%s output=%s",
                    func.__name__,
                    call_time,
                    duration_ms,
                    params,
                    result_preview,
                )

        def _log_exception(logger, call_time, start, params, exc: Exception):
            """原函数抛异常时记录耗时及异常追踪，包装函数随后重新抛出原异常。

            Args:
                logger (Any): 记录当前工具调用的日志对象。
                call_time (Any): 当前工具开始执行时的 ISO 时间。
                start (Any): 工具开始执行时的 perf_counter 值。
                params (Any): 经过敏感字段过滤的调用参数。
                exc (Exception): 退出上下文时的异常对象。
            """
            duration_ms = int((time.perf_counter() - start) * 1000)
            logger.exception(
                "tool=%s event=exception call_time=%s duration_ms=%s params=%s error=%s",
                func.__name__,
                call_time,
                duration_ms,
                params,
                shorten_for_log(str(exc), max_len=500),
            )

        # 2. 异步函数必须等待原调用结束后再记录结果；同步函数沿用同步异常和返回行为。
        if inspect.iscoroutinefunction(func):
            @functools.wraps(func)
            async def async_wrapper(*args, **kwargs):
                """调用异步工具前记录开始，等待完成后记录结果，异常记日志后继续向调用方抛出。

                Args:
                    args (Any): 当前工具调用或被包装函数的参数。
                    kwargs (Any): 传递给被调用函数的其他关键字参数。

                Returns:
                    Any: 原异步工具的返回值。
                """
                logger, call_time, start, params = _prepare_log_context(args, kwargs)
                try:
                    result = await func(*args, **kwargs)
                    _log_success_or_error(logger, call_time, start, params, result)
                    return result
                except Exception as exc:
                    _log_exception(logger, call_time, start, params, exc)
                    raise

            return async_wrapper

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            """调用同步工具前记录开始，结束后记录结果，异常记日志后继续向调用方抛出。

            Args:
                args (Any): 当前工具调用或被包装函数的参数。
                kwargs (Any): 传递给被调用函数的其他关键字参数。

            Returns:
                Any: 原同步工具的返回值。
            """
            logger, call_time, start, params = _prepare_log_context(args, kwargs)
            try:
                result = func(*args, **kwargs)
                _log_success_or_error(logger, call_time, start, params, result)
                return result
            except Exception as exc:
                _log_exception(logger, call_time, start, params, exc)
                raise

        return wrapper

    return decorator
