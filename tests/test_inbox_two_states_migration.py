"""验证离线迁移保留已交付记录、任务通知及编号，不触碰其他表。"""

import sqlite3

import pytest

from scripts.migrate_inbox_two_states import migrate


def test_check_is_read_only_and_apply_preserves_facts_and_sequence(tmp_path) -> None:
    """检查只读，迁移删除旧意图，保留任务事实和自增上界。

    Args:
        tmp_path (Path): 测试专用目录。
    """
    path = tmp_path / "legacy.sqlite3"
    # 1. 创建独立旧表，覆盖已交付记录、旧真人意图、任务事实和较高自增编号。
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE session_inbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
            kind TEXT NOT NULL, origin_channel TEXT NOT NULL, payload_json TEXT NOT NULL,
            delivery_mode TEXT DEFAULT 'immediate', queue_ready INTEGER DEFAULT 0,
            status TEXT CHECK(status IN ('pending','claimed','delivered')),
            claim_token TEXT, created_at TEXT, claimed_at TEXT, delivered_at TEXT)""")
        for number, kind, status in ((1, "human", "delivered"), (2, "human", "pending"),
                                     (3, "human", "claimed"), (4, "task_terminal", "claimed"),
                                     (5, "task_terminal", "pending")):
            db.execute("INSERT INTO session_inbox VALUES(?, 's', ?, 'desktop', '{}', 'immediate', 0, ?, 'old', 'created', 'claimed', 'delivered')",
                       (number, kind, status))
        db.execute("UPDATE sqlite_sequence SET seq = 100 WHERE name = 'session_inbox'")
        db.execute("CREATE TABLE checkpoints(id INTEGER PRIMARY KEY, state_json TEXT)")
        db.execute("INSERT INTO checkpoints VALUES(1, 'unchanged')")
        db.execute("CREATE TABLE conversation_messages(id INTEGER PRIMARY KEY, message_json TEXT)")
        db.execute("INSERT INTO conversation_messages VALUES(1, 'history')")
    before = path.read_bytes()
    # 2. 检查模式和缺少停机声明的应用操作都不能改变文件。
    report = migrate(path)
    assert report["needs_migration"]
    assert path.read_bytes() == before
    with pytest.raises(ValueError, match="停机"):
        migrate(path, apply=True)
    migrate(path, apply=True, confirm_app_stopped=True)
    # 3. 应用后只有 Inbox 改变，其他持久事实、记录编号和自增上界保持。
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT id, kind, status FROM session_inbox ORDER BY id").fetchall() == [
            (1, "human", "delivered"), (4, "task_terminal", "pending"), (5, "task_terminal", "pending"),
        ]
        assert db.execute("SELECT * FROM checkpoints").fetchall() == [(1, "unchanged")]
        assert db.execute("SELECT * FROM conversation_messages").fetchall() == [(1, "history")]
        assert db.execute("SELECT seq FROM sqlite_sequence WHERE name='session_inbox'").fetchone()[0] == 100
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert not migrate(path)["needs_migration"]
    # 4. 新表重复检查或应用均不再次修改数据文件。
    after = path.read_bytes()
    migrate(path, apply=True, confirm_app_stopped=True)
    assert path.read_bytes() == after
