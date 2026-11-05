"""离线迁移只处理明确指定的测试数据库，并保持跨表升级原子性。"""

import sqlite3

import pytest

from scripts.migrate_plugin_runtime import migrate


@pytest.mark.parametrize("invalid_history", [False, True])
def test_offline_upgrade_preserves_records_and_rolls_back_all_tables(tmp_path, invalid_history):
    """检查模式不写入，升级保留历史编号，任一表失败时撤销全部结构修改。"""
    path = tmp_path / "state.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE task_records (task_id TEXT PRIMARY KEY, session_id TEXT, command TEXT NOT NULL, status TEXT, pid INTEGER, exit_code INTEGER, output_path TEXT NOT NULL, created_at TEXT, ended_at TEXT, origin_run_id TEXT, origin_tool_call_id TEXT, output_ref TEXT, background INTEGER, tool_name TEXT)")
        db.execute("INSERT INTO task_records VALUES ('old','s','echo hello','completed',1,0,'old.log','2026-01-01',NULL,'r','c','old',1,'exec_command')")
        db.execute("CREATE TABLE session_inbox (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, kind TEXT CHECK(kind IN ('human','task_terminal')), origin_channel TEXT, payload_json TEXT, delivery_mode TEXT, queue_ready INTEGER, status TEXT, created_at TEXT, delivered_at TEXT)")
        db.execute("INSERT INTO session_inbox VALUES (42,'s','task_terminal','system','{}','immediate',0,'pending','2026-01-01',NULL)")
        db.execute("CREATE TABLE conversation_messages (id INTEGER PRIMARY KEY AUTOINCREMENT, message_type TEXT CHECK(message_type IN ('human','legacy')), message_json TEXT)")
        db.execute("CREATE INDEX history_type ON conversation_messages(message_type)")
        db.execute("INSERT INTO conversation_messages VALUES (19,?,'{}')", ("legacy" if invalid_history else "human",))
        before = db.execute("SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
    assert migrate(path) == {"tables": ["task_records", "session_inbox", "conversation_messages"], "applied": False}
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name").fetchall() == before
    if invalid_history:
        with pytest.raises(sqlite3.IntegrityError):
            migrate(path, apply=True)
        with sqlite3.connect(path) as db:
            assert db.execute("SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name").fetchall() == before
        return
    assert migrate(path, apply=True)["applied"]
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT task_id,command,title,name,notify_agent FROM task_records").fetchone() == ("old", "echo hello", "echo hello", "terminal", 1)
        assert db.execute("SELECT id FROM session_inbox").fetchall() == [(42,)]
        assert db.execute("SELECT id FROM conversation_messages").fetchall() == [(19,)]
        assert db.execute("SELECT name FROM sqlite_master WHERE name='history_type'").fetchone()
        db.execute("INSERT INTO conversation_messages(message_type,message_json) VALUES ('plugin_notification','{}')")
        assert db.execute("SELECT MAX(id) FROM conversation_messages").fetchone() == (20,)
    assert migrate(path)["tables"] == []
