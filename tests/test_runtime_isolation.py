from pathlib import Path

import pytest

from app.environment import (
    PRODUCTION_DATA_DIR,
    get_chat_history_db,
    get_chat_settings_file,
    get_data_dir,
    get_memory_base_dir,
)


def test_test_paths_are_inside_disposable_data_dir() -> None:
    """验证测试存储路径全部位于一次性目录内。"""
    data_dir = get_data_dir()

    assert data_dir != PRODUCTION_DATA_DIR.resolve()
    assert get_chat_history_db().is_relative_to(data_dir)
    assert get_chat_settings_file().is_relative_to(data_dir)


def test_test_mode_requires_explicit_data_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    """验证测试模式必须显式配置数据目录。

    Args:
        monkeypatch (pytest.MonkeyPatch): Pytest 提供的替换工具，用例结束后恢复环境变量和依赖。
    """
    monkeypatch.delenv("AYAYA_DATA_DIR")

    with pytest.raises(RuntimeError, match="必须设置 AYAYA_DATA_DIR"):
        get_data_dir()


def test_test_mode_rejects_production_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    """验证测试模式拒绝生产 memory 目录。

    Args:
        monkeypatch (pytest.MonkeyPatch): Pytest 提供的替换工具，用例结束后恢复环境变量和依赖。
    """
    monkeypatch.setenv("AYAYA_DATA_DIR", str(PRODUCTION_DATA_DIR))

    with pytest.raises(RuntimeError, match="生产 memory/ 目录之外"):
        get_data_dir()


def test_memory_override_must_stay_in_test_data_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """验证记忆路径覆盖值不得越过测试目录。

    Args:
        monkeypatch (pytest.MonkeyPatch): Pytest 提供的替换工具，用例结束后恢复环境变量和依赖。
        tmp_path (Path): Pytest 为当前用例创建的临时目录，不访问生产存储。
    """
    monkeypatch.setenv("MEMORY_BASE_PATH", str(tmp_path))

    with pytest.raises(RuntimeError, match="必须位于 AYAYA_DATA_DIR 内"):
        get_memory_base_dir()
