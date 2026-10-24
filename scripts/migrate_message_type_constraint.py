"""停机后收紧原始对话历史的消息类型约束，完整保留已有记录。

先使用 --db 路径 --check，备份并关闭应用后再使用 --apply --confirm-app-stopped。
本次迁移只重建 conversation_messages 的 CHECK 约束；失败时整笔事务回滚。
需要撤销已完成迁移时，在应用关闭的情况下恢复迁移前的 SQLite 备份。
"""

from __future__ import annotations

import argparse
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3
import sys


# 1. 固定本次迁移的目标，后续新增消息类型时另建迁移，不改已经应用的脚本。
ALLOWED_TYPES = ("human", "assistant", "tool", "task_result", "subagent")
TYPE_CHECK = re.compile(r"CHECK\s*\(\s*message_type\s+IN\s*\(([^)]+)\)\s*\)", re.IGNORECASE)
REPLACEMENT_TABLE = "conversation_messages_type_constraint"


def migrate(path: Path, *, apply: bool = False, confirm_app_stopped: bool = False) -> dict:
    """只读检查或在一个事务中更新历史表约束，不筛除或改写历史记录。

    Args:
        path (Path): 明确指定、已经存在的 SQLite 数据库。
        apply (bool): 是否实际重建表，默认只读检查。
        confirm_app_stopped (bool): 应用模式下确认数据库使用方已经停止。

    Returns:
        dict: 消息类型计数、自增编号、约束和完整性检查结果，不包含消息正文。

    Raises:
        ValueError: 未确认停机、表结构不符或存在目标约束不允许的历史记录。
        sqlite3.Error: 数据库操作失败，重建事务回滚。
    """
    if apply and not confirm_app_stopped:
        raise ValueError("应用迁移必须确认停机")
    mode = "rw" if apply else "ro"
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=" + mode, uri=True)) as db:
        # 1. 外键检查在表替换结束后统一执行；应用模式持有写事务，检查模式保持只读。
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("BEGIN IMMEDIATE" if apply else "BEGIN")
        try:
            row = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='conversation_messages'").fetchone()
            if row is None:
                raise ValueError("目标没有 conversation_messages 表")
            schema = row[0]
            matches = list(TYPE_CHECK.finditer(schema))
            if len(matches) != 1:
                raise ValueError("没有找到唯一的 message_type IN 约束")
            old_types = tuple(re.findall(r"'([^']+)'", matches[0].group(1)))
            counts = dict(db.execute("SELECT message_type,COUNT(*) FROM conversation_messages GROUP BY message_type"))
            sequence = db.execute("SELECT seq FROM sqlite_sequence WHERE name='conversation_messages'").fetchone()
            high = sequence[0] if sequence else 0
            report = {"needs_migration": set(old_types) != set(ALLOWED_TYPES),
                      "rows": sum(counts.values()), "message_types": counts,
                      "sequence": high, "allowed_types": list(ALLOWED_TYPES), "applied": False}
            unsupported = {kind: count for kind, count in counts.items() if kind is not None and kind not in ALLOWED_TYPES}
            if unsupported:
                raise ValueError(f"存在目标约束不允许的历史记录，保留原表：{unsupported}")
            if apply and report["needs_migration"]:
                # 2. 只替换类型约束和临时表名，其余列、唯一约束及存储声明沿用原定义。
                allowed = ",".join(f"'{kind}'" for kind in ALLOWED_TYPES)
                updated = TYPE_CHECK.sub(f"CHECK(message_type IN ({allowed}))", schema)
                updated, replacements = re.subn(
                    r'^(CREATE\s+TABLE\s+)(?:"conversation_messages"|conversation_messages)(?=\s|\()',
                    rf'\1"{REPLACEMENT_TABLE}"', updated, count=1, flags=re.IGNORECASE,
                )
                if replacements != 1:
                    raise ValueError("原表声明不符合本次迁移格式")
                objects = db.execute("SELECT sql FROM sqlite_master WHERE tbl_name='conversation_messages' AND type IN ('index','trigger') AND sql IS NOT NULL").fetchall()
                columns = ",".join(f'"{row[1]}"' for row in db.execute("PRAGMA table_info(conversation_messages)"))
                db.execute(updated)
                db.execute(f'INSERT INTO "{REPLACEMENT_TABLE}" ({columns}) SELECT {columns} FROM conversation_messages')
                # 3. 数据复制成功后替换表，并恢复索引、触发器和曾经发出过的最大编号。
                db.execute("DROP TABLE conversation_messages")
                db.execute(f'ALTER TABLE "{REPLACEMENT_TABLE}" RENAME TO conversation_messages')
                db.execute("DELETE FROM sqlite_sequence WHERE name='conversation_messages'")
                db.execute("INSERT INTO sqlite_sequence(name,seq) VALUES('conversation_messages',?)", (high,))
                for (sql,) in objects:
                    db.execute(sql)
                report["applied"] = True
            # 4. 完整性与外键检查通过后才提交；只读检查结束时回滚读取事务。
            integrity = db.execute("PRAGMA integrity_check").fetchall()
            if integrity != [("ok",)] or db.execute("PRAGMA foreign_key_check").fetchall():
                raise sqlite3.IntegrityError("迁移完整性或外键检查失败")
            report["integrity"] = "ok"
            db.commit() if apply else db.rollback()
            return report
        except BaseException:
            db.rollback()
            raise


def main() -> int:
    """解析显式目标与停机声明，输出不包含消息正文的检查报告。

    Returns:
        int: 成功为 0，失败且未提交修改为 1。
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
        print(f"迁移终止，未提交修改：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
