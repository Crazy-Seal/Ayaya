"""运行环境与存储路径隔离。

桌面端正常运行时默认使用生产环境。测试必须显式设置 ``AYAYA_ENV=test``，
并提供隔离的 ``AYAYA_DATA_DIR``；禁止测试静默回退到生产 ``memory`` 目录。
"""

from __future__ import annotations

import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_DATA_DIR = PROJECT_ROOT / "memory"
PRODUCTION_CHAT_SETTINGS_FILE = PROJECT_ROOT / "config" / "chat_settings.yaml"
PRODUCTION_MCP_SETTINGS_FILE = PROJECT_ROOT / "config" / "mcp_servers.yaml"
PRODUCTION_DESKTOP_SETTINGS_FILE = PROJECT_ROOT / "config" / "desktop_settings.yaml"


def get_desktop_settings_file() -> Path:
    """选择桌面 YAML，测试覆盖路径也必须位于隔离数据目录。

    Returns:
        Path: 生产配置路径或本次测试配置路径。
    """
    configured = os.getenv("AYAYA_DESKTOP_SETTINGS_FILE", "").strip()
    path = Path(configured) if configured else (
        get_data_dir() / "config" / "desktop_settings.yaml"
        if is_test_environment() else PRODUCTION_DESKTOP_SETTINGS_FILE
    )
    return require_test_storage_path(path, "AYAYA_DESKTOP_SETTINGS_FILE") if is_test_environment() else _resolved(path)


def get_legacy_frontend_settings_file() -> Path:
    """只在迁移时选择前端 JSON，测试不得回退到生产 ui/user_data。

    Returns:
        Path: 现有前端数据根目录中的旧配置文件。
    """
    configured = os.getenv("AYAYA_UI_DATA_DIR", "").strip()
    directory = Path(configured) if configured else (
        get_data_dir() / "ui" if is_test_environment() else PROJECT_ROOT / "ui" / "user_data"
    )
    path = directory / "frontend_settings.json"
    return require_test_storage_path(path, "AYAYA_UI_DATA_DIR") if is_test_environment() else _resolved(path)


def get_environment() -> str:
    """读取并规范化运行环境名称，未配置时按桌面生产环境运行。

    Returns:
        str: production 或 test。

    Raises:
        RuntimeError: 环境名称不受支持。
    """
    value = os.getenv("AYAYA_ENV", "production").strip().lower()
    aliases = {"prod": "production", "testing": "test"}
    value = aliases.get(value, value)
    if value not in {"production", "test"}:
        raise RuntimeError("AYAYA_ENV 必须设置为 'production' 或 'test'")
    return value


def is_test_environment() -> bool:
    """判断当前进程是否显式使用测试环境。

    Returns:
        bool: 规范化环境名为 test 时为 True。
    """
    return get_environment() == "test"


def _resolved(path: Path) -> Path:
    """展开用户目录并解析绝对路径，供存储隔离检查使用。

    Args:
        path (Path): 要读取或写入的文件路径。

    Returns:
        Path: 规范化后的绝对路径。
    """
    return path.expanduser().resolve()


def _is_within(path: Path, parent: Path) -> bool:
    """比较解析后的路径，确认目标等于父目录或位于其内部。

    Args:
        path (Path): 要读取或写入的文件路径。
        parent (Path): 允许包含目标路径的父目录。

    Returns:
        bool: 目标在父目录范围内时为 True。
    """
    path = _resolved(path)
    parent = _resolved(parent)
    return path == parent or path.is_relative_to(parent)


def get_data_dir() -> Path:
    """选择当前环境的数据根目录，测试环境必须显式使用生产 memory 之外的目录。

    Returns:
        Path: 当前运行数据的绝对根目录，不在此创建目录。

    Raises:
        RuntimeError: 测试未配置数据目录，或目录位于生产 memory 内。
    """
    configured = os.getenv("AYAYA_DATA_DIR", "").strip()
    if is_test_environment():
        if not configured:
            raise RuntimeError(
                "AYAYA_ENV=test 时必须设置 AYAYA_DATA_DIR；"
                "测试不得回退到生产存储"
            )
        data_dir = _resolved(Path(configured))
        if _is_within(data_dir, PRODUCTION_DATA_DIR):
            raise RuntimeError("测试数据目录必须位于生产 memory/ 目录之外")
        return data_dir

    return _resolved(Path(configured)) if configured else PRODUCTION_DATA_DIR


def require_test_storage_path(path: Path, label: str) -> Path:
    """解析存储路径，测试环境下拒绝隔离数据根目录之外的覆盖值。

    Args:
        path (Path): 要读取或写入的文件路径。
        label (str): 发生路径隔离错误时显示的配置名称。

    Returns:
        Path: 已检查的绝对存储路径。

    Raises:
        RuntimeError: 测试存储路径越过 AYAYA_DATA_DIR。
    """
    resolved = _resolved(path)
    if is_test_environment() and not _is_within(resolved, get_data_dir()):
        raise RuntimeError(f"测试模式下 {label} 必须位于 AYAYA_DATA_DIR 内")
    return resolved


def get_memory_base_dir() -> Path:
    """解析 MEMORY_BASE_PATH 覆盖值，省略时使用当前数据根目录。

    Returns:
        Path: 经过测试隔离检查的记忆根目录。
    """
    configured = os.getenv("MEMORY_BASE_PATH", "").strip()
    path = Path(configured) if configured else get_data_dir()
    return require_test_storage_path(path, "MEMORY_BASE_PATH")


def get_mem0_qdrant_dir(memory_base_dir: Path) -> Path:
    """解析语义记忆向量目录，测试环境覆盖值仍须位于隔离数据目录内。

    Args:
        memory_base_dir (Path): 当前环境的记忆数据根目录。

    Returns:
        Path: Mem0/Qdrant 的当前持久目录。
    """
    configured = os.getenv("MEM0_QDRANT_PATH", "").strip()
    path = Path(configured) if configured else memory_base_dir / "mem0" / "qdrant_data"
    return require_test_storage_path(path, "MEM0_QDRANT_PATH")


def get_chat_settings_file() -> Path:
    """选择会话设置 YAML，测试默认从隔离目录读取，生产默认从仓库 config 读取。

    Returns:
        Path: 当前环境使用的设置文件路径。
    """
    configured = os.getenv("AYAYA_CHAT_SETTINGS_FILE", "").strip()
    if configured:
        path = _resolved(Path(configured))
    elif is_test_environment():
        path = get_data_dir() / "config" / "chat_settings.yaml"
    else:
        path = PRODUCTION_CHAT_SETTINGS_FILE

    if is_test_environment():
        return require_test_storage_path(path, "AYAYA_CHAT_SETTINGS_FILE")
    return path


def get_mcp_settings_file() -> Path:
    """解析当前运行环境使用的 MCP 配置文件路径。

    Returns:
        Path: 生产或隔离测试环境中的 MCP Server YAML 路径。
    """
    configured = os.getenv("AYAYA_MCP_SETTINGS_FILE", "").strip()
    if configured:
        path = _resolved(Path(configured))
    elif is_test_environment():
        path = get_data_dir() / "config" / "mcp_servers.yaml"
    else:
        path = PRODUCTION_MCP_SETTINGS_FILE

    if is_test_environment():
        return require_test_storage_path(path, "AYAYA_MCP_SETTINGS_FILE")
    return path


def get_sqlite_dir() -> Path:
    """提供当前环境数据根目录下的 sqlite 子目录。

    Returns:
        Path: SQLite 文件所在目录，不在此创建目录。
    """
    return get_data_dir() / "sqlite"


def get_agent_state_db() -> Path:
    """返回 Agent checkpoint 与聊天记录共用的 SQLite 文件。"""
    return get_sqlite_dir() / "agent_checkpoints.sqlite3"


def get_chat_history_db() -> Path:
    """返回聊天历史共用的状态数据库，避免历史与 checkpoint 分开提交。

    Returns:
        Path: agent_checkpoints.sqlite3 的当前环境路径。
    """
    return get_agent_state_db()


def get_checkpoint_db() -> Path:
    """返回会话 checkpoint 使用的共享状态数据库。

    Returns:
        Path: 与对话协议日志相同的 SQLite 文件路径。
    """
    return get_agent_state_db()


def get_images_dir() -> Path:
    """返回当前环境保存真人附件和工具图片的目录。

    Returns:
        Path: 数据根目录下的 images 路径。
    """
    return get_data_dir() / "images"


def get_tool_results_dir() -> Path:
    """返回当前运行环境中按会话保存完整工具结果的根目录。"""
    return get_data_dir() / "tool_results"
