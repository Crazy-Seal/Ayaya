"""离线整理子 Agent 和任务存储；默认只读检查，--apply 才提交。"""

import argparse
from datetime import datetime, timezone
from contextlib import closing
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.crud.agent_storage_sql import agent_state_schema
from app.crud.session_inbox_dao import INBOX_SCHEMA_SQL
from app.crud.subagent_sql import SUBAGENT_SCHEMA_SQL
from app.crud.task_record_dao import TASK_SCHEMA_SQL
from app.schemas.agent_storage import AgentStorageScope

TASK_ADDITIONS = {
    "subagent_id": "TEXT", "notification_context_id": "TEXT",
    "notification_scope": "TEXT NOT NULL DEFAULT 'main' CHECK(notification_scope IN ('main','subagent'))",
    "kind": "TEXT NOT NULL DEFAULT 'ordinary' CHECK(kind IN ('ordinary','subagent'))", "end_reason": "TEXT",
}
CHILD_TABLES = ("subagents", "subagent_interactions", "subagent_checkpoints",
                "subagent_session_inbox", "subagent_conversation_messages")


def _table_sql(db: sqlite3.Connection, name: str) -> str | None:
    """读取当前表定义，缺表时返回 None。"""
    row = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
    return str(row[0]) if row else None


def _upgrade_inbox(db: sqlite3.Connection) -> None:
    """事务内替换 Inbox 约束，保留数据、索引、触发器和自增高水位。"""
    columns = [str(row[1]) for row in db.execute("PRAGMA table_info(session_inbox)")]
    if "claimed_at" in columns or "queue_ready" not in columns:
        raise ValueError("请先执行 migrate_inbox_two_states.py 升级旧 Inbox")
    objects = db.execute("SELECT sql FROM sqlite_master WHERE tbl_name='session_inbox' "
                         "AND type IN ('index','trigger') AND sql IS NOT NULL").fetchall()
    sequence = db.execute("SELECT seq FROM sqlite_sequence WHERE name='session_inbox'").fetchone()
    target = INBOX_SCHEMA_SQL.replace("session_inbox", "session_inbox_subagent_new", 1)
    db.execute(target)
    names = ",".join(f'"{name}"' for name in columns)
    db.execute(f"INSERT INTO session_inbox_subagent_new ({names}) SELECT {names} FROM session_inbox")
    db.execute("DROP TABLE session_inbox")
    db.execute("ALTER TABLE session_inbox_subagent_new RENAME TO session_inbox")
    for (statement,) in objects:
        db.execute(statement)
    if sequence is not None:
        db.execute("UPDATE sqlite_sequence SET seq=? WHERE name='session_inbox'", sequence)


def migrate(path: Path, *, apply: bool = False) -> dict[str, object]:
    """检查或升级明确指定的离线数据库，失败回滚全部改动。

    Args:
        path (Path): 必须已存在的 SQLite 文件，脚本不寻找生产路径。
        apply (bool): 默认为只读检查，显式为 true 才修改。

    Returns:
        dict[str, object]: 缺失项目和是否提交。
    """
    mode = "rw" if apply else "ro"
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=" + mode, uri=True)) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("BEGIN IMMEDIATE" if apply else "BEGIN")
        try:
            missing = [table for table in CHILD_TABLES if _table_sql(db, table) is None]
            subagent_columns = {str(row[1]) for row in db.execute("PRAGMA table_info(subagents)")}
            rename_retirement = "released_at" in subagent_columns
            remove_destroy_flag = "destroy_requested" in subagent_columns
            if rename_retirement or remove_destroy_flag:
                missing.append("subagents")
            task_sql = _table_sql(db, "task_records")
            columns = {str(row[1]) for row in db.execute("PRAGMA table_info(task_records)")} if task_sql else set()
            additions = [column for column in TASK_ADDITIONS if column not in columns]
            remove_task_lifetime = "lifetime" in columns
            if additions or remove_task_lifetime:
                missing.append("task_records")
            inbox_sql = _table_sql(db, "session_inbox")
            upgrade_inbox = inbox_sql is not None and "'subagent'" not in inbox_sql
            if upgrade_inbox or inbox_sql is None:
                missing.append("session_inbox")
            if not apply:
                db.rollback()
                return {"tables": missing, "applied": False}
            # 1. 补齐任务来源并删除旧运行期限列，保留任务数据及其余索引和触发器。
            if task_sql is None:
                db.execute(TASK_SCHEMA_SQL)
            else:
                if "notify_agent" not in columns:
                    raise ValueError("请先执行 migrate_plugin_runtime.py 升级旧任务表")
                for column in additions:
                    db.execute(f"ALTER TABLE task_records ADD COLUMN {column} {TASK_ADDITIONS[column]}")
                if remove_task_lifetime:
                    db.execute("ALTER TABLE task_records DROP COLUMN lifetime")
            # 2. 只有 CHECK 约束需要替表；新子表与主数据同事务建立。
            if upgrade_inbox:
                _upgrade_inbox(db)
            else:
                db.execute(INBOX_SCHEMA_SQL)
            # 2.1. 原位改名保留废弃时间及引用该列的索引、触发器，和本次迁移一起提交。
            if rename_retirement:
                db.execute("ALTER TABLE subagents RENAME COLUMN released_at TO retired_at")
            # 2.2. 将旧销毁意图转换成首次废弃时间，再删除重复字段；失败与其他升级一起回滚。
            if remove_destroy_flag:
                db.execute("UPDATE subagents SET retired_at=? WHERE destroy_requested=1 AND retired_at IS NULL",
                           (datetime.now(timezone.utc).isoformat(),))
                db.execute("ALTER TABLE subagents DROP COLUMN destroy_requested")
            db.execute(INBOX_SCHEMA_SQL.replace("session_inbox", "subagent_session_inbox"))
            for statement in agent_state_schema(AgentStorageScope.SUBAGENT):
                db.execute(statement)
            for statement in SUBAGENT_SCHEMA_SQL.split(";"):
                if statement.strip():
                    db.execute(statement)
            # 3. 检查完整性后才提交，重复执行不会重新复制或删除记录。
            if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)] or db.execute("PRAGMA foreign_key_check").fetchall():
                raise sqlite3.IntegrityError("子 Agent 迁移完整性检查失败")
            db.commit()
            return {"tables": missing, "applied": bool(missing)}
        except BaseException:
            db.rollback()
            raise


def main() -> None:
    """解析显式文件路径及 --apply，默认只打印检查结果。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(migrate(args.database, apply=args.apply))


if __name__ == "__main__":
    main()
