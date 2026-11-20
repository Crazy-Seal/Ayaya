"""用隔离数据库验证计数搬迁、原事务边界和旧版本升级链。"""

from copy import deepcopy
import json
from pathlib import Path
import sqlite3

import pytest

from app.agent.state import AgentState
from app.crud.agent_storage_sql import agent_state_schema
from app.schemas.agent_storage import AgentStorageScope
from scripts.migrate_memory_state_v5 import convert_checkpoint, migrate
from tests.memory_support import memory_count


def old_checkpoint(count: int) -> dict:
    """准备包含其他插件状态和协议消息的版本 4 快照。

    Args:
        count (int): 旧版已经保存的真人计数。

    Returns:
        dict: 待迁移的旧状态。
    """
    state = AgentState.create_new("migration")
    state.add_user_message("保留已有消息")
    raw = state.to_checkpoint()
    raw.update(schema_version=4, long_term_memory_turn_counter=count)
    raw["extra"] = {"other": {"value": [1, 2]}, "memory": {"reserved": "保留"}}
    return raw


def create_database(path: Path, *, invalid: bool = False) -> list[dict]:
    """准备同时包含主、子历史与无关业务数据的隔离库。

    Args:
        path (Path): 测试数据库。
        invalid (bool): 是否在子表中放置冲突计数。

    Returns:
        list[dict]: 两张表中各自的原始状态。
    """
    original = [old_checkpoint(10), old_checkpoint(7)]
    if invalid:
        original[1]["extra"]["memory"]["long_term_memory_turn_counter"] = 2
    with sqlite3.connect(path) as db:
        for scope, raw in zip(AgentStorageScope, original):
            for sql in agent_state_schema(scope):
                db.execute(sql)
            db.execute(f"INSERT INTO {scope.checkpoints_table}(session_id,state_json,checkpoint_type) VALUES(?,?,'completed')",
                       (raw["session_id"], json.dumps(raw)))
        db.execute("CREATE TABLE untouched(value TEXT)")
        db.execute("INSERT INTO untouched VALUES('原业务数据')")
        db.execute("PRAGMA user_version=4")
    return original


def test_migration_preserves_counters_other_fields_and_is_repeatable(tmp_path: Path) -> None:
    """预检查无写入，迁移后逐条恢复原计数，重复执行没有待变更记录。"""
    path = tmp_path / "checkpoints.sqlite3"
    original = create_database(path)
    before = path.read_bytes()
    assert migrate(path)["changed_rows"] == 2
    assert path.read_bytes() == before
    with pytest.raises(ValueError, match="停止后端"):
        migrate(path, apply=True)
    report = migrate(path, apply=True, confirm_app_stopped=True)
    assert report["integrity"] == "ok"
    assert Path(report["backup"]).is_file()
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 5
        assert db.execute("SELECT value FROM untouched").fetchone()[0] == "原业务数据"
        for scope, old in zip(AgentStorageScope, original):
            raw = json.loads(db.execute(f"SELECT state_json FROM {scope.checkpoints_table}").fetchone()[0])
            assert memory_count(AgentState.from_checkpoint(raw)) == old["long_term_memory_turn_counter"]
            expected = deepcopy(old)
            expected["schema_version"] = 5
            expected["extra"]["memory"]["long_term_memory_turn_counter"] = expected.pop("long_term_memory_turn_counter")
            assert raw == expected
    assert migrate(path)["changed_rows"] == 0
    assert migrate(path, apply=True, confirm_app_stopped=True)["changed_rows"] == 0


def test_conflict_in_child_keeps_both_tables_at_previous_version(tmp_path: Path) -> None:
    """第二张表有冲突时，第一张表的计数及数据库版本也保持原值。"""
    path = tmp_path / "checkpoints.sqlite3"
    original = create_database(path, invalid=True)
    with pytest.raises(ValueError, match="冲突"):
        migrate(path, apply=True, confirm_app_stopped=True)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 4
        for scope, raw in zip(AgentStorageScope, original):
            assert json.loads(db.execute(f"SELECT state_json FROM {scope.checkpoints_table}").fetchone()[0]) == raw


def test_write_failure_rolls_back_preceding_updates(tmp_path: Path) -> None:
    """写入第二张表失败时，先写入的主状态及数据库版本一起回滚。"""
    path = tmp_path / "checkpoints.sqlite3"
    original = create_database(path)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TRIGGER fail_update BEFORE UPDATE ON subagent_checkpoints "
                   "BEGIN SELECT RAISE(ABORT,'test write failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="test write failure"):
        migrate(path, apply=True, confirm_app_stopped=True)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 4
        for scope, raw in zip(AgentStorageScope, original):
            assert json.loads(db.execute(f"SELECT state_json FROM {scope.checkpoints_table}").fetchone()[0]) == raw


@pytest.mark.parametrize("counter", [-1, True, "7", None, 1.5])
def test_invalid_counter_is_rejected(counter) -> None:
    """损坏计数不会被转成零或覆盖其他状态。"""
    raw = old_checkpoint(counter)
    with pytest.raises(ValueError, match="非负整数"):
        convert_checkpoint(raw)


def test_image_v3_to_v4_then_memory_v5_upgrade_chain(tmp_path: Path) -> None:
    """历史图片迁移仍生成版本 4，再由新迁移升级到可恢复的版本 5。"""
    from tests.test_image_storage_v4_migration import make_v3_fixture
    from scripts.migrate_image_storage_v4 import migrate as migrate_images

    path, images = tmp_path / "state.sqlite3", tmp_path / "images"
    raw = make_v3_fixture(path, images)
    migrate_images(path, images, apply=True)
    migrate(path, apply=True, confirm_app_stopped=True)
    with sqlite3.connect(path) as db:
        saved = json.loads(db.execute("SELECT state_json FROM checkpoints").fetchone()[0])
    restored = AgentState.from_checkpoint(saved)
    assert restored.messages[0].message_id == raw["messages"][0]["message_id"]
    assert memory_count(restored) == 0
