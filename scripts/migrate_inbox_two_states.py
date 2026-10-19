"""停机后将 Inbox 改为 pending/delivered；未交付真人意图作废，不可逆。

先使用 --db 路径 --check；关闭应用后才能使用 --apply --confirm-app-stopped。
脚本只改 Inbox，失败回滚；运行前可用 SQLite backup 保存需要保留的原始数据库。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent.runtime.session_inbox import INBOX_SCHEMA_SQL


def migrate(path: Path, *, apply: bool = False, confirm_app_stopped: bool = False) -> dict:
    """检查或原子迁移明确指定的数据库，不读取默认生产路径。

    Args:
        path (Path): 必须已经存在的目标 SQLite 文件。
        apply (bool): 是否执行表重建，默认只读检查。
        confirm_app_stopped (bool): 应用模式要求调用方确认已停机。

    Returns:
        dict: 是否需要迁移、原始行数、应用结果及完整性检查。

    Raises:
        ValueError: 未确认停机或目标没有 Inbox 表。
        sqlite3.Error: 数据或结构不合法，应用事务回滚。
    """
    if apply and not confirm_app_stopped:
        raise ValueError("应用迁移必须确认停机")
    mode = "rw" if apply else "ro"
    with sqlite3.connect(path.resolve().as_uri() + "?mode=" + mode, uri=True) as db:
        schema = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='session_inbox'").fetchone()
        if schema is None:
            raise ValueError("目标数据库没有 session_inbox 表")
        needs_migration = "claimed" in schema[0]
        report = {"needs_migration": needs_migration,
                  "rows": db.execute("SELECT COUNT(*) FROM session_inbox").fetchone()[0], "applied": False}
        if not apply or not needs_migration:
            report["integrity"] = db.execute("PRAGMA integrity_check").fetchone()[0]
            return report
        try:
            # 1. 迁移期间独占写事务，所有表重建和数据筛选一起提交或一起回滚。
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT seq FROM sqlite_sequence WHERE name='session_inbox'").fetchone()
            high = row[0] if row else 0
            db.execute(INBOX_SCHEMA_SQL.replace("IF NOT EXISTS session_inbox", "session_inbox_two_states"))
            # 2. 真人旧意图作废；任务终态是事实，未交付通知回到 pending。
            db.execute("""INSERT INTO session_inbox_two_states
                (id,session_id,kind,origin_channel,payload_json,delivery_mode,queue_ready,status,created_at,delivered_at)
                SELECT id,session_id,kind,origin_channel,payload_json,delivery_mode,queue_ready,
                    CASE WHEN status='claimed' THEN 'pending' ELSE status END,
                    created_at,CASE WHEN status='delivered' THEN delivered_at ELSE NULL END
                FROM session_inbox WHERE kind<>'human' OR status='delivered'""")
            db.execute("DROP TABLE session_inbox")
            db.execute("ALTER TABLE session_inbox_two_states RENAME TO session_inbox")
            # 3. 即使所有真人输入被删除，也保留已经发出过的编号上界。
            db.execute("DELETE FROM sqlite_sequence WHERE name='session_inbox'")
            db.execute("INSERT INTO sqlite_sequence(name,seq) VALUES('session_inbox',?)", (high,))
            db.execute("CREATE INDEX idx_session_inbox_order ON session_inbox(session_id,status,id)")
            integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise sqlite3.IntegrityError("迁移后完整性检查失败")
            db.commit()
            report.update(applied=True, integrity=integrity)
            return report
        except BaseException:
            db.rollback()
            raise


def main() -> int:
    """解析显式路径、只读检查或停机应用选项，输出不包含正文的报告。

    Returns:
        int: 成功为 0，失败为 1。
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path)
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--check", action="store_true")
    actions.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-app-stopped", action="store_true")
    args = parser.parse_args()
    try:
        report = migrate(args.db, apply=args.apply, confirm_app_stopped=args.confirm_app_stopped)
        print(json.dumps(report, ensure_ascii=False))
        return 0
    except (ValueError, sqlite3.Error) as exc:
        print(f"迁移终止，未提交数据：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
