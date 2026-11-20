"""停机后把 checkpoint 的记忆计数迁入插件命名空间，默认只读预检查。"""

from __future__ import annotations

import argparse
from copy import deepcopy
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

SOURCE_VERSION = 4
TARGET_VERSION = 5
CHECKPOINT_TABLES = ("checkpoints", "subagent_checkpoints")
COUNTER_KEY = "long_term_memory_turn_counter"


def convert_checkpoint(raw: dict) -> dict:
    """搬迁一个 checkpoint 的计数，保留其他业务字段和记忆命名空间数据。

    Args:
        raw (dict): 已保存的版本 4 或版本 5 状态。

    Returns:
        dict: 独立的版本 5 状态。

    Raises:
        ValueError: 版本、命名空间或计数无效，或新旧计数冲突。
    """
    version = raw.get("schema_version")
    if version not in (SOURCE_VERSION, TARGET_VERSION):
        raise ValueError(f"不支持的 checkpoint 版本：{version!r}")
    result = deepcopy(raw)
    extra = result.setdefault("extra", {})
    if not isinstance(extra, dict):
        raise ValueError("checkpoint.extra 必须是对象")
    namespace = extra.setdefault("memory", {})
    if not isinstance(namespace, dict):
        raise ValueError("checkpoint.extra.memory 必须是对象")
    if version == TARGET_VERSION:
        if COUNTER_KEY in result:
            raise ValueError("版本 5 checkpoint 仍有顶层记忆计数")
        counter = namespace.get(COUNTER_KEY, 0)
    else:
        if COUNTER_KEY not in result:
            raise ValueError("版本 4 checkpoint 缺少记忆计数")
        counter = result.pop(COUNTER_KEY)
        if COUNTER_KEY in namespace and (type(namespace[COUNTER_KEY]) is not int or namespace[COUNTER_KEY] != counter):
            raise ValueError("新旧记忆计数冲突")
        namespace[COUNTER_KEY] = counter
        result["schema_version"] = TARGET_VERSION
    if type(counter) is not int or counter < 0:
        raise ValueError("记忆计数必须是非负整数")
    # 1. 已升级状态沿用原字节语义，首次创建的状态可以尚无记忆命名空间。
    return raw if version == TARGET_VERSION else result


def migrate(db_path: Path, *, apply: bool = False, confirm_app_stopped: bool = False) -> dict:
    """检查全部主子 checkpoint，备份后在同一事务中执行计数搬迁。

    Args:
        db_path (Path): 显式指定的现有 checkpoint 数据库。
        apply (bool): 是否备份并执行迁移，默认只读。
        confirm_app_stopped (bool): 调用者是否已停止后端。

    Returns:
        dict: 各表检查数量、变更数量、备份位置和完整性结果。

    Raises:
        ValueError: 未确认停机、版本或数据无效，数据库保持原状。
        sqlite3.Error: 数据库检查或写入失败，事务回滚。
    """
    db_path = db_path.resolve(strict=True)
    if apply and not confirm_app_stopped:
        raise ValueError("执行迁移前请停止后端，并提供 --confirm-app-stopped")
    mode = "rw" if apply else "ro"
    connection = sqlite3.connect(f"{db_path.as_uri()}?mode={mode}", uri=True)
    report = {"table_rows": {}, "changed_rows": 0, "backup": None, "applied": apply}
    try:
        if apply:
            backup = db_path.with_name(f"{db_path.name}.before-memory-v5-{datetime.now(timezone.utc):%Y%m%dT%H%M%S%fZ}.bak")
            with closing(sqlite3.connect(backup)) as target:
                connection.backup(target)
            report["backup"] = str(backup)
            connection.execute("BEGIN IMMEDIATE")
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version not in (SOURCE_VERSION, TARGET_VERSION):
            raise ValueError(f"数据库 user_version={version}，需要先迁移到版本 4")
        updates = []
        # 1. 先完整转换和验证两张表，任何一条错误都会终止整次迁移。
        for table in CHECKPOINT_TABLES:
            exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
            if not exists:
                raise ValueError(f"缺少 checkpoint 表：{table}")
            rows = list(connection.execute(f"SELECT id, state_json FROM {table}"))
            report["table_rows"][table] = len(rows)
            for row_id, payload in rows:
                raw = json.loads(payload)
                converted = convert_checkpoint(raw)
                if converted != raw:
                    updates.append((table, row_id, json.dumps(converted, ensure_ascii=False)))
        report["changed_rows"] = len(updates)
        # 2. 数据变换与版本标记一起提交，消息表和其他业务表保持原值。
        if apply:
            for table, row_id, payload in updates:
                connection.execute(f"UPDATE {table} SET state_json=? WHERE id=?", (payload, row_id))
                saved = connection.execute(f"SELECT state_json FROM {table} WHERE id=?", (row_id,)).fetchone()
                if saved is None or json.loads(saved[0]) != json.loads(payload):
                    raise ValueError(f"迁移写入校验失败：{table} id={row_id}")
            connection.execute(f"PRAGMA user_version={TARGET_VERSION}")
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok" or list(connection.execute("PRAGMA foreign_key_check")):
            raise ValueError("数据库完整性检查失败")
        report["integrity"] = integrity
        if apply:
            connection.commit()
        return report
    except BaseException:
        if apply:
            connection.rollback()
        raise
    finally:
        connection.close()


def main() -> None:
    """从命令行检查或迁移显式指定的现有数据库，并输出统计。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-app-stopped", action="store_true")
    args = parser.parse_args()
    print(json.dumps(migrate(args.db, apply=args.apply, confirm_app_stopped=args.confirm_app_stopped), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
