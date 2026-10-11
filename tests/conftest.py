"""在导入应用模块前，强制所有后端测试使用一次性存储目录。"""

import logging
import os
import tempfile
from pathlib import Path


_TEST_STORAGE = tempfile.TemporaryDirectory(prefix="ayaya-tests-")
os.environ["AYAYA_ENV"] = "test"
os.environ["AYAYA_DATA_DIR"] = _TEST_STORAGE.name
os.environ["AYAYA_API_TOKEN"] = "t" * 43
os.environ["MEMORY_EXTRACTION_MAX_CONTEXT_TOKENS"] = "128000"

# 生产环境的路径覆盖值不得泄漏到测试进程中。
for variable in (
    "AYAYA_CHAT_SETTINGS_FILE",
    "AYAYA_MCP_SETTINGS_FILE",
    "AYAYA_DESKTOP_SETTINGS_FILE",
    "AYAYA_UI_DATA_DIR",
    "AYAYA_PARENT_PID",
    "MEMORY_BASE_PATH",
    "MEM0_QDRANT_PATH",
):
    os.environ.pop(variable, None)


def pytest_sessionfinish(session, exitstatus) -> None:
    """由 Pytest 在整轮测试结束后调用，关闭测试工具日志句柄并清理一次性存储目录。

    Args:
        session (pytest.Session): Pytest 当前测试会话，用于结束阶段的资源清理。
        exitstatus (int): Pytest 本次执行的退出状态。
    """
    tool_logger = logging.getLogger("app.agent.tools")
    test_tool_log = (Path(_TEST_STORAGE.name) / "logs" / "tools.log").resolve()
    for handler in tuple(tool_logger.handlers):
        if (
            isinstance(handler, logging.FileHandler)
            and Path(handler.baseFilename).resolve() == test_tool_log
        ):
            tool_logger.removeHandler(handler)
            handler.close()
    _TEST_STORAGE.cleanup()
