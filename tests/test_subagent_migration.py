"""验证显式离线迁移保留原数据和数据库附属对象。"""

import sqlite3
from pathlib import Path

import pytest

from app.crud.session_inbox_dao import INBOX_SCHEMA_SQL
from scripts.migrate_subagents import migrate


@pytest.mark.parametrize("invalid_reference", [False, True])
def test_task_lifetime_removal_preserves_records_and_rolls_back_on_failure(
    tmp_path: Path, invalid_reference: bool,
) -> None:
    """删除旧任务期限列时保留任务及附属对象，最后校验失败则恢复原结构和数据。

    Args:
        tmp_path (Path): 本次测试独占的离线数据库目录。
        invalid_reference (bool): 是否加入无效外键，使最后的校验失败。
    """
    path = tmp_path / "task-lifetime.sqlite3"
    with sqlite3.connect(path):
        pass
    migrate(path, apply=True)
    with sqlite3.connect(path) as db:
        db.execute("ALTER TABLE task_records ADD COLUMN lifetime TEXT NOT NULL DEFAULT 'independent'")
        db.execute("INSERT INTO task_records(task_id,session_id,status,created_at,name,title,origin_run_id,"
                   "plugin_name,notify_agent,result,lifetime) VALUES "
                   "('task','parent','completed','created','work','原任务','run','sample',1,'已有结果','independent')")
        db.execute("CREATE INDEX custom_task_origin ON task_records(origin_run_id)")
        db.execute("CREATE TABLE task_audit(value TEXT)")
        db.execute("CREATE TRIGGER custom_task_update AFTER UPDATE OF title ON task_records "
                   "BEGIN INSERT INTO task_audit VALUES(new.title); END")
        if invalid_reference:
            db.execute("CREATE TABLE identity_record(id TEXT PRIMARY KEY)")
            db.execute("CREATE TABLE dangling_task(id TEXT REFERENCES identity_record(id))")
            db.execute("INSERT INTO dangling_task VALUES('missing')")
        columns = [row[1] for row in db.execute("PRAGMA table_info(task_records)") if row[1] != "lifetime"]
        selection = ",".join(columns)
        original = db.execute(f"SELECT {selection} FROM task_records").fetchall()
    before = path.read_bytes()
    assert "task_records" in migrate(path)["tables"]
    assert path.read_bytes() == before
    if invalid_reference:
        with pytest.raises(sqlite3.IntegrityError, match="完整性检查失败"):
            migrate(path, apply=True)
    else:
        assert migrate(path, apply=True)["applied"]
        assert migrate(path)["tables"] == []
        assert not migrate(path, apply=True)["applied"]
    with sqlite3.connect(path) as db:
        assert db.execute(f"SELECT {selection} FROM task_records").fetchall() == original
        assert ("lifetime" in {row[1] for row in db.execute("PRAGMA table_info(task_records)")}) == invalid_reference
        assert db.execute("PRAGMA index_info(custom_task_origin)").fetchone()[2] == "origin_run_id"
        db.execute("UPDATE task_records SET title='更新任务' WHERE task_id='task'")
        assert db.execute("SELECT value FROM task_audit").fetchall() == [("更新任务",)]
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def legacy_database(path: Path) -> None:
    """建立迁移前的 Inbox、自增高水位和自定义索引、触发器。"""
    with sqlite3.connect(path) as db:
        db.execute(INBOX_SCHEMA_SQL.replace(
            "'human', 'task_terminal', 'plugin_notification', 'agent_command', 'subagent', 'parent_reply'",
            "'human', 'task_terminal', 'plugin_notification'"))
        db.execute("CREATE TABLE task_records(task_id TEXT PRIMARY KEY, notify_agent INTEGER)")
        db.execute("CREATE TABLE audit(value TEXT)")
        db.execute("CREATE INDEX custom_inbox ON session_inbox(created_at)")
        db.execute("CREATE TRIGGER custom_trigger AFTER INSERT ON session_inbox BEGIN INSERT INTO audit VALUES(new.session_id); END")
        db.execute("INSERT INTO session_inbox(id,session_id,kind,payload_json,created_at) VALUES(40,'parent','human','{}','date')")
        db.execute("INSERT INTO session_inbox(id,session_id,kind,payload_json,created_at) VALUES(90,'deleted','human','{}','date')")
        db.execute("DELETE FROM session_inbox WHERE id=90")


def test_subagent_migration_is_explicit_preserves_objects_and_is_repeatable(tmp_path: Path) -> None:
    """只读检查不写数据库，应用后保留行、触发器、索引和已删除行的编号高水位。"""
    path = tmp_path / "legacy.sqlite3"
    legacy_database(path)
    before = path.read_bytes()
    assert migrate(path)["applied"] is False
    assert path.read_bytes() == before
    assert migrate(path, apply=True)["applied"] is True
    assert migrate(path)["tables"] == []
    assert migrate(path, apply=True)["applied"] is False
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT id,session_id FROM session_inbox").fetchall() == [(40, "parent")]
        db.execute("INSERT INTO session_inbox(session_id,kind,payload_json,created_at) VALUES('child','subagent','{}','date')")
        assert db.execute("SELECT max(id) FROM session_inbox").fetchone() == (91,)
        assert db.execute("SELECT value FROM audit ORDER BY rowid DESC LIMIT 1").fetchone() == ("child",)
        assert db.execute("SELECT name FROM sqlite_master WHERE name='custom_inbox'").fetchone()
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def test_subagent_migration_rolls_back_when_existing_schema_is_invalid(tmp_path: Path) -> None:
    """前置升级不满足时，任务表已追加的字段也随事务回滚。"""
    path = tmp_path / "legacy.sqlite3"
    legacy_database(path)
    with sqlite3.connect(path) as db:
        db.execute("ALTER TABLE session_inbox ADD COLUMN claimed_at TEXT")
    with pytest.raises(ValueError, match="migrate_inbox_two_states"):
        migrate(path, apply=True)
    with sqlite3.connect(path) as db:
        assert [row[1] for row in db.execute("PRAGMA table_info(task_records)")] == ["task_id", "notify_agent"]
        assert db.execute("SELECT count(*) FROM session_inbox").fetchone() == (1,)


def test_retirement_column_migration_keeps_values_indexes_and_triggers(tmp_path: Path) -> None:
    """旧废弃时间列原位改名，保留数据及实际可用的索引和触发器。

    Args:
        tmp_path (Path): 隔离数据库目录。
    """
    path = tmp_path / "existing.sqlite3"
    with sqlite3.connect(path):
        pass
    migrate(path, apply=True)
    with sqlite3.connect(path) as db:
        db.execute("ALTER TABLE subagents RENAME COLUMN retired_at TO released_at")
        db.execute("INSERT INTO subagents VALUES('child','parent','助手','测试','[]',1,'created','retired')")
        db.execute("CREATE INDEX custom_retirement ON subagents(released_at)")
        db.execute("CREATE TABLE audit(value TEXT)")
        db.execute("CREATE TRIGGER retirement_audit AFTER UPDATE OF released_at ON subagents "
                   "BEGIN INSERT INTO audit VALUES(new.released_at); END")
    before = path.read_bytes()
    assert "subagents" in migrate(path)["tables"]
    assert path.read_bytes() == before
    assert migrate(path, apply=True)["applied"] is True
    assert migrate(path)["tables"] == []
    assert migrate(path, apply=True)["applied"] is False
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT retired_at FROM subagents WHERE subagent_id='child'").fetchone() == ("retired",)
        assert db.execute("PRAGMA index_info(custom_retirement)").fetchone()[2] == "retired_at"
        db.execute("UPDATE subagents SET retired_at='later' WHERE subagent_id='child'")
        assert db.execute("SELECT value FROM audit").fetchall() == [("later",)]
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def test_retirement_column_rename_rolls_back_when_integrity_check_fails(tmp_path: Path) -> None:
    """迁移最后的完整性检查失败时，废弃列改名也随事务撤销。

    Args:
        tmp_path (Path): 隔离数据库目录。
    """
    path = tmp_path / "invalid.sqlite3"
    with sqlite3.connect(path):
        pass
    migrate(path, apply=True)
    with sqlite3.connect(path) as db:
        db.execute("ALTER TABLE subagents RENAME COLUMN retired_at TO released_at")
        db.execute("CREATE TABLE parent_identity(id TEXT PRIMARY KEY)")
        db.execute("CREATE TABLE dangling_reference(id TEXT REFERENCES parent_identity(id))")
        db.execute("INSERT INTO dangling_reference VALUES('missing')")
    with pytest.raises(sqlite3.IntegrityError, match="完整性检查失败"):
        migrate(path, apply=True)
    with sqlite3.connect(path) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(subagents)")}
        assert "released_at" in columns
        assert "retired_at" not in columns


def test_legacy_destroy_flag_becomes_first_retirement_time_and_is_dropped(tmp_path: Path) -> None:
    """旧销毁标记转换为废弃时间，已存在时间保留，默认检查不改数据。"""
    from datetime import datetime, timezone
    path = tmp_path / "flag.sqlite3"
    with sqlite3.connect(path):
        pass
    migrate(path, apply=True)
    with sqlite3.connect(path) as db:
        db.execute("ALTER TABLE subagents ADD COLUMN destroy_requested INTEGER NOT NULL DEFAULT 0 CHECK(destroy_requested IN (0,1))")
        db.executemany("INSERT INTO subagents VALUES(?, 'parent', '助手', '测试', '[]', 1, 'created', ?, ?)",
            [("old", "2026-01-01T00:00:00+00:00", 1), ("destroyed", None, 1), ("active", None, 0)])
    before = path.read_bytes()
    assert "subagents" in migrate(path)["tables"]
    assert path.read_bytes() == before
    start = datetime.now(timezone.utc)
    migrate(path, apply=True)
    with sqlite3.connect(path) as db:
        values = dict(db.execute("SELECT subagent_id, retired_at FROM subagents"))
        assert values["old"] == "2026-01-01T00:00:00+00:00"
        assert values["active"] is None
        assert start <= datetime.fromisoformat(values["destroyed"]) <= datetime.now(timezone.utc)
        assert "destroy_requested" not in {row[1] for row in db.execute("PRAGMA table_info(subagents)")}
    assert migrate(path)["tables"] == []
