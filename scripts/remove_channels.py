"""停机后一次性删除渠道结构；显式指定数据库，检查模式保持文件只读。"""

from __future__ import annotations

import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
from typing import TypedDict


class MigrationReport(TypedDict):
    """记录渠道结构清理的数量及数据库完整性。

    Attributes:
        applied (bool): 本次是否以应用模式完成事务。
        tables_dropped (list[str]): 需要删除的渠道订阅表。
        columns_removed (list[str]): 需要删除渠道列的主子 Inbox 表。
        messages_updated (int): 需要清理的消息记录数量。
        checkpoints_updated (int): 需要清理的 checkpoint 数量。
        integrity (str): SQLite 完整性检查结果。
    """

    applied: bool
    tables_dropped: list[str]
    columns_removed: list[str]
    messages_updated: int
    checkpoints_updated: int
    integrity: str


def clean_message(message: dict[str, object]) -> bool:
    """删除真人或模型消息顶层的渠道字段，保留消息内容和工具参数。

    Args:
        message (dict[str, object]): 当前格式的单条持久化消息。

    Returns:
        bool: 本条消息确实删除了渠道字段时为真。
    """
    if message.get("message_type") in {"human", "assistant"} and "origin_channel" in message:
        del message["origin_channel"]
        return True
    return False


def clean_checkpoint(state: dict[str, object]) -> bool:
    """清理 checkpoint 中的消息渠道和当前审批渠道。

    Args:
        state (dict[str, object]): 一条完整 checkpoint，插件状态和工具恢复材料原样保留。

    Returns:
        bool: 消息或审批材料的框架字段发生清理时为真。
    """
    changed = False
    messages = state.get("messages")
    if isinstance(messages, list):
        for message in messages:
            if isinstance(message, dict):
                changed = clean_message(message) or changed
    interrupt = state.get("interrupt_data")
    if isinstance(interrupt, dict) and "origin_channel" in interrupt:
        del interrupt["origin_channel"]
        changed = True
    return changed


def migrate(path: Path, *, apply: bool = False) -> MigrationReport:
    """在同一事务清理渠道结构，失败后回滚全部表和记录修改。

    应用迁移前须结束使用该数据库的后端。脚本通过显式路径打开已有文件，
    检查模式统计待清理内容；应用模式同时清理主、子存储。

    Args:
        path (Path): 调用方明确指定的已有 SQLite 数据库。
        apply (bool): 为真时写入并提交，否则仅以只读方式检查。

    Returns:
        MigrationReport: 待清理数量、应用状态及完整性检查结果。

    Raises:
        ValueError: 消息或 checkpoint 的 JSON 结构无效。
        sqlite3.Error: 表结构或数据操作失败，事务整体回滚。
    """
    report: MigrationReport = {"applied": False, "tables_dropped": [], "columns_removed": [],
        "messages_updated": 0, "checkpoints_updated": 0, "integrity": ""}
    mode = "rw" if apply else "ro"
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=" + mode, uri=True)) as db:
        try:
            # 1. 获取一致数据库视图；应用模式在修改前取得写事务。
            db.execute("BEGIN IMMEDIATE" if apply else "BEGIN")
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "channel_subscriptions" in tables:
                report["tables_dropped"].append("channel_subscriptions")
                if apply:
                    db.execute("DROP TABLE channel_subscriptions")
            # 2. 直接删除渠道列，SQLite 保留其余列、记录、索引和自增编号上界。
            for table in ("session_inbox", "subagent_session_inbox"):
                if table in tables and any(row[1] == "origin_channel" for row in db.execute(f"PRAGMA table_info({table})")):
                    report["columns_removed"].append(table)
                    if apply:
                        db.execute(f"ALTER TABLE {table} DROP COLUMN origin_channel")
            # 3. 只清理固定业务位置，正文、工具参数及插件附加状态保持原值。
            for table in ("conversation_messages", "subagent_conversation_messages", "checkpoints", "subagent_checkpoints"):
                if table not in tables:
                    continue
                checkpoint = table.endswith("checkpoints")
                column = "state_json" if checkpoint else "message_json"
                for identifier, raw in db.execute(f"SELECT id,{column} FROM {table}").fetchall():
                    value = json.loads(raw)
                    if not isinstance(value, dict):
                        raise ValueError(f"{table} 第 {identifier} 条记录的 JSON 必须是对象")
                    changed = clean_checkpoint(value) if checkpoint else clean_message(value)
                    if changed:
                        if checkpoint:
                            report["checkpoints_updated"] += 1
                        else:
                            report["messages_updated"] += 1
                        if apply:
                            db.execute(f"UPDATE {table} SET {column}=? WHERE id=?",
                                (json.dumps(value, ensure_ascii=False), identifier))
            # 4. 完整性检查通过后提交，检查模式结束只读事务。
            report["integrity"] = db.execute("PRAGMA integrity_check").fetchone()[0]
            if report["integrity"] != "ok":
                raise sqlite3.IntegrityError("迁移后数据库完整性检查失败")
            if apply:
                db.commit()
                report["applied"] = True
            else:
                db.rollback()
            return report
        except BaseException:
            db.rollback()
            raise


def main() -> int:
    """解析显式路径和检查或应用选项，输出不包含正文的迁移报告。

    Returns:
        int: 成功为零，迁移检查或写入失败时为一。
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--check", action="store_true")
    actions.add_argument("--apply", action="store_true")
    arguments = parser.parse_args()
    try:
        print(json.dumps(migrate(arguments.db, apply=arguments.apply), ensure_ascii=False))
        return 0
    except (ValueError, sqlite3.Error) as exc:
        print(f"渠道迁移终止，未提交修改：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
