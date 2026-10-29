import ast
import asyncio
import logging
from pathlib import Path

import pytest

from app.agent.state.messages import ToolCall
from app.agent.utils.infra.log import _ensure_tool_file_handler, log_tool_call_result
from app.environment import get_data_dir


def test_tool_file_handler_uses_disposable_test_data_dir() -> None:
    """验证工具日志文件只使用一次性测试目录。"""
    logger = logging.getLogger("tests.tool_log_isolation")

    _ensure_tool_file_handler(logger)
    file_handlers = [
        handler
        for handler in logger.handlers
        if isinstance(handler, logging.FileHandler)
    ]

    try:
        assert len(file_handlers) == 1
        assert Path(file_handlers[0].baseFilename) == (
            get_data_dir() / "logs" / "tools.log"
        )
    finally:
        for handler in file_handlers:
            logger.removeHandler(handler)
            handler.close()


@pytest.mark.parametrize("frozen", [False, True])
def test_tool_log_preserves_nested_fields_and_sanitizes_values(
    caplog: pytest.LogCaptureFixture, frozen: bool,
) -> None:
    """普通参数和审批后冻结的参数都应记录动作字段，并递归脱敏及截断。

    Args:
        caplog (pytest.LogCaptureFixture): 捕获实际日志输出，检查最终记录的参数。
        frozen (bool): 是否使用 ToolCall 递归冻结后的工具参数。
    """
    original = {
        "password": "test-root-credential",
        "actions": [{
            "target": "测试按钮",
            "bbox": [10, 20, 30, 40],
            "metadata": {
                "Authorization": "test-nested-credential",
                "note": "x" * 510,
                "history": list(range(25)),
            },
        }],
    }
    call = ToolCall(id="test-log", name="control_screen", args=original)
    params = call.args if frozen else original

    with caplog.at_level(logging.INFO, logger="app.agent.tools"):
        result = asyncio.run(log_tool_call_result(call.name, params, "完成"))

    record = caplog.records[-1].getMessage()
    logged_params = ast.literal_eval(record.split(" params=", 1)[1].split(" output=", 1)[0])
    assert logged_params == {
        "password": "***",
        "actions": [{
            "target": "测试按钮",
            "bbox": [10, 20, 30, 40],
            "metadata": {
                "Authorization": "***",
                "note": "x" * 500 + "...",
                "history": list(range(20)),
            },
        }],
    }
    assert "test-root-credential" not in record
    assert "test-nested-credential" not in record
    assert result == "完成"
    assert call.to_dict()["args"] == original
