"""验证历史类型约束迁移保留原记录、自增编号和索引。"""

from pathlib import Path
import sqlite3

import pytest

from scripts.migrate_message_type_constraint import migrate


def create_database(path: Path, *, environment_row: bool = False) -> None:
    """创建含旧约束和历史编号空缺的隔离数据库。

    Args:
        path (Path): Pytest 临时目录内的数据库路径。
        environment_row (bool): 是否插入新约束无法接受的环境消息。
    """
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE conversation_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, message_id TEXT NOT NULL,
            created_at TEXT NOT NULL, record_kind TEXT NOT NULL, fidelity TEXT NOT NULL, legacy_source_id TEXT,
            message_type TEXT CHECK(message_type IN ('human','assistant','tool','task_result','subagent','environment_perception')),
            message_json TEXT NOT NULL, UNIQUE(session_id,message_id))""")
        db.execute("CREATE INDEX history_order ON conversation_messages(session_id,id)")
        db.executemany("INSERT INTO conversation_messages VALUES (?,?,?,?,?,?,?,?,?)", [
            (3, "s", "human", "2026-10-24", "protocol", "exact", None, "human", '{"text":"原始正文"}'),
            (7, "s", "legacy", "2026-10-24", "legacy_tool_notice", "projected", "old", None, '{}'),
        ])
        if environment_row:
            db.execute("INSERT INTO conversation_messages VALUES (8,'s','env','2026-10-24','protocol','exact',NULL,'environment_perception','{}')")
        db.execute("UPDATE sqlite_sequence SET seq=100 WHERE name='conversation_messages'")


def test_constraint_migration_preserves_rows_sequence_and_is_idempotent(tmp_path: Path) -> None:
    """检查不写库，应用后保留数据和索引并拒绝环境消息，重复应用不再重建。

    Args:
        tmp_path (Path): Pytest 提供的隔离目录。
    """
    path = tmp_path / "history.sqlite3"
    create_database(path)
    before = path.read_bytes()
    assert migrate(path)["needs_migration"]
    assert path.read_bytes() == before
    with sqlite3.connect(path) as db:
        rows = db.execute("SELECT * FROM conversation_messages ORDER BY id").fetchall()
    assert migrate(path, apply=True, confirm_app_stopped=True)["applied"]
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT * FROM conversation_messages ORDER BY id").fetchall() == rows
        assert db.execute("SELECT seq FROM sqlite_sequence WHERE name='conversation_messages'").fetchone() == (100,)
        assert db.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='history_order'").fetchone()
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            db.execute("INSERT INTO conversation_messages VALUES (101,'s','new','2026-10-24','protocol','exact',NULL,'environment_perception','{}')")
        db.execute("INSERT INTO conversation_messages VALUES (NULL,'s','next','2026-10-24','protocol','exact',NULL,'assistant','{}')")
        assert db.execute("SELECT id FROM conversation_messages WHERE message_id='next'").fetchone() == (101,)
    assert not migrate(path, apply=True, confirm_app_stopped=True)["applied"]


def test_disallowed_existing_history_prevents_changes(tmp_path: Path) -> None:
    """已有环境消息时拒绝收紧约束，原数据和表定义均保持不变。

    Args:
        tmp_path (Path): Pytest 提供的隔离目录。
    """
    path = tmp_path / "history.sqlite3"
    create_database(path, environment_row=True)
    before = path.read_bytes()
    with pytest.raises(ValueError, match="保留原表"):
        migrate(path, apply=True, confirm_app_stopped=True)
    assert path.read_bytes() == before


def test_app_stopped_confirmation_is_required(tmp_path: Path) -> None:
    """未确认停机时拒绝写入，连数据库连接也不创建。

    Args:
        tmp_path (Path): Pytest 提供的隔离目录。
    """
    path = tmp_path / "missing.sqlite3"
    with pytest.raises(ValueError, match="停机"):
        migrate(path, apply=True)
    assert not path.exists()
