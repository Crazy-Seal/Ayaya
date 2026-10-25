"""离线升级通用任务及插件通知表；默认只检查，指定 --apply 才写入目标文件。"""

import argparse
from contextlib import closing
from pathlib import Path
import re
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.crud.task_record_dao import TASK_SCHEMA_SQL
from app.crud.session_inbox_dao import INBOX_SCHEMA_SQL
from app.agent.state.messages import PROTOCOL_MESSAGE_TYPES
from app.schemas.task_types import TERMINAL_TASK_NAME
from app.schemas.runtime_types import TaskLifetime


def migrate(path: Path, *, apply: bool = False) -> dict:
    """在单个事务内替换目标表结构，保留记录、索引、触发器和自增编号。

    Args:
        path (Path): 已存在、由调用方明确指定的 SQLite 文件。
        apply (bool): 为真时提交升级，否则只报告需要升级的表。

    Returns:
        dict: 需要升级的表名和是否完成写入。
    """
    # 1. 检查时以只读方式打开指定数据库，应用升级时取得写权限；路径错误时直接报错。
    mode = "rw" if apply else "ro"
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=" + mode, uri=True)) as db:
        # 1.1. 替表期间暂时关闭外键强制检查，全部表处理完成后统一检查引用是否完整。
        db.execute("PRAGMA foreign_keys=OFF")
        # 1.2. 三张表共用一个事务；写入模式提前取得写锁，避免迁移期间其他连接修改数据。
        db.execute("BEGIN IMMEDIATE" if apply else "BEGIN")
        changed = []
        try:
            # 2. 逐表检查旧结构，只有需要新增任务字段或允许插件通知的表才进入升级流程。
            for table, target in (("task_records", TASK_SCHEMA_SQL), ("session_inbox", INBOX_SCHEMA_SQL), ("conversation_messages", None)):
                row = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
                # 2.1. 数据库尚未创建的表留给应用初始化，迁移只处理已有数据表。
                if row is None:
                    continue
                source = row[0]
                columns = [item[1] for item in db.execute(f'PRAGMA table_info("{table}")')]
                # 2.2. 任务表以新增的通知开关字段判断版本；Inbox 和历史表以插件通知类型判断。
                needed = "notify_agent" not in columns if table == "task_records" else "plugin_notification" not in source
                if not needed:
                    continue
                changed.append(table)
                # 2.3. 检查模式只收集待升级表名，继续检查后面的表。
                if not apply:
                    continue
                # 2.4. 历史表沿用原建表定义，只替换消息类型约束，保留其他列及约束。
                if table == "conversation_messages":
                    allowed = ",".join(f"'{kind.value}'" for kind in PROTOCOL_MESSAGE_TYPES)
                    target, count = re.subn(r"CHECK\s*\(\s*message_type\s+IN\s*\([^)]*\)\s*\)", f"CHECK(message_type IN ({allowed}))", source, flags=re.I)
                    # 2.4.1. 约束结构与预期不符时终止迁移，由统一异常处理回滚此前的表修改。
                    if count != 1:
                        raise ValueError("历史消息类型约束不是预期结构")
                temporary = f"{table}_plugin_runtime_new"
                declaration = re.sub(r"^CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[\w\"]+", f'CREATE TABLE "{temporary}"', target.strip(), count=1, flags=re.I)
                # 2.5. 替换旧表前保存显式索引、触发器和自增高水位，供新表恢复使用。
                # 表定义中的主键和唯一约束会随新表创建相应的隐式索引。
                objects = db.execute("SELECT sql FROM sqlite_master WHERE tbl_name=? AND type IN ('index','trigger') AND sql IS NOT NULL", (table,)).fetchall()
                sequence = db.execute("SELECT seq FROM sqlite_sequence WHERE name=?", (table,)).fetchone() if db.execute("SELECT 1 FROM sqlite_master WHERE name='sqlite_sequence'").fetchone() else None
                # 2.6. 创建目标结构的临时表，旧列原样复制；新增字段按旧终端任务的业务含义补值。
                # 标题沿用命令，任务保持独立运行并发送结果通知，输出引用继续使用原任务编号。
                db.execute(declaration)
                new_columns = [item[1] for item in db.execute(f'PRAGMA table_info("{temporary}")')]
                defaults = {"name": f"'{TERMINAL_TASK_NAME}'", "title": '"command"', "lifetime": f"'{TaskLifetime.INDEPENDENT.value}'", "notify_agent": "1", "background": "1", "output_ref": '"task_id"'}
                select = [f'"{name}"' if name in columns else defaults.get(name, "NULL") for name in new_columns]
                names = ",".join(f'"{name}"' for name in new_columns)
                # 2.7. 先复制完整数据，确认新约束接受已有记录后再删除旧表；复制失败会整体回滚。
                db.execute(f'INSERT INTO "{temporary}" ({names}) SELECT {",".join(select)} FROM "{table}"')
                db.execute(f'DROP TABLE "{table}"')
                db.execute(f'ALTER TABLE "{temporary}" RENAME TO "{table}"')
                # 2.8. 恢复原索引和触发器，并保留曾分配的最大自增编号，避免复用已删除记录的编号。
                for (statement,) in objects:
                    db.execute(statement)
                if sequence:
                    db.execute("UPDATE sqlite_sequence SET seq=? WHERE name=?", (sequence[0], table))
            # 3. 统一校验数据库结构和外键引用，任何检查失败都撤销本次所有表的升级。
            if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)] or db.execute("PRAGMA foreign_key_check").fetchall():
                raise sqlite3.IntegrityError("迁移完整性检查失败")
            # 4. 应用模式提交全部修改；检查模式结束只读事务，返回待升级表及实际写入状态。
            db.commit() if apply else db.rollback()
            return {"tables": changed, "applied": apply and bool(changed)}
        except BaseException:
            # 5. 写入、检查失败或操作被中断时统一回滚，让调用方收到原始异常。
            db.rollback()
            raise


def main() -> None:
    """解析显式数据库路径，输出检查或升级结果。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(migrate(args.db_path, apply=args.apply))


if __name__ == "__main__":
    main()
