"""供根 DAO 在同一事务中复用子身份核对及显式交互 SQL。"""

from datetime import datetime, timezone

import aiosqlite

from app.schemas.subagent_types import SubagentInteractionKind

SUBAGENT_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS subagents (
    subagent_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, name TEXT NOT NULL,
    system_prompt TEXT NOT NULL, tool_whitelist_json TEXT NOT NULL,
    if_retain_context INTEGER NOT NULL CHECK(if_retain_context IN (0,1)),
    created_at TEXT NOT NULL, retired_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_subagents_parent ON subagents(session_id, retired_at);
CREATE TABLE IF NOT EXISTS subagent_interactions (
    interaction_id INTEGER PRIMARY KEY AUTOINCREMENT, subagent_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('task','steer','reply','a2a','final')),
    text TEXT NOT NULL, created_at TEXT NOT NULL, source_id TEXT NOT NULL,
    UNIQUE(subagent_id, kind, source_id)
);
CREATE INDEX IF NOT EXISTS idx_subagent_interactions ON subagent_interactions(subagent_id, interaction_id);
"""


async def ensure_subagent_schema(db: aiosqlite.Connection) -> None:
    """检查结构版本并创建子元数据表，保留调用方的事务所有权。

    Args:
        db (aiosqlite.Connection): 调用方负责提交或回滚的数据库连接。

    Raises:
        RuntimeError: 现有表仍使用旧的废弃时间列，需要先离线迁移。
    """
    columns = {str(row[1]) for row in await db.execute_fetchall("PRAGMA table_info(subagents)")}
    if "released_at" in columns or "destroy_requested" in columns:
        raise RuntimeError("subagents 需要先执行 scripts/migrate_subagents.py 离线迁移")
    for statement in SUBAGENT_SCHEMA_SQL.split(";"):
        if statement.strip():
            await db.execute(statement)


async def append_interaction_in_transaction(db: aiosqlite.Connection, subagent_id: str,
                                           kind: SubagentInteractionKind, text: str, source_id: str) -> None:
    """随 Inbox 或最终报告事务记录显式交互，同来源重试保持幂等。"""
    await db.execute(
        "INSERT INTO subagent_interactions(subagent_id,kind,text,created_at,source_id) VALUES (?,?,?,?,?) "
        "ON CONFLICT(subagent_id,kind,source_id) DO NOTHING",
        (subagent_id, kind.value, text, datetime.now(timezone.utc).isoformat(), source_id),
    )


async def require_subagent_in_transaction(db: aiosqlite.Connection, session_id: str, subagent_id: str) -> None:
    """在写事务内核对父子归属及废弃时间，与身份废弃串行。"""
    if not await subagent_available_in_transaction(db, session_id, subagent_id):
        raise ValueError("子 Agent 已废弃或父子归属不匹配")


async def subagent_available_in_transaction(db: aiosqlite.Connection, session_id: str, subagent_id: str) -> bool:
    """在调用方事务内读取子归属及废弃时间，供登记和结果投递共用。"""
    return bool(await db.execute_fetchall(
        "SELECT 1 FROM subagents WHERE session_id=? AND subagent_id=? AND retired_at IS NULL",
        (session_id, subagent_id),
    ))


async def interaction_exists_in_transaction(
    db: aiosqlite.Connection, subagent_id: str, source_id: str,
    kinds: tuple[SubagentInteractionKind, ...],
) -> bool:
    """在同一事务内按固定来源核对整项消息是否已投递。"""
    return await read_interaction_kind_in_transaction(db, subagent_id, source_id, kinds) is not None


async def read_interaction_kind_in_transaction(
    db: aiosqlite.Connection, subagent_id: str, source_id: str,
    kinds: tuple[SubagentInteractionKind, ...],
) -> SubagentInteractionKind | None:
    """在调用方事务内读取固定来源的原受理类别，重放时保留原有含义。

    Args:
        db (aiosqlite.Connection): 调用方已经取得的事务连接。
        subagent_id (str): 显式交互所属子身份。
        source_id (str): 重试期间固定的消息来源。
        kinds (tuple[SubagentInteractionKind, ...]): 作为同项业务统一去重的类别。

    Returns:
        SubagentInteractionKind | None: 已提交的原类别，尚未受理时为空。
    """
    placeholders = ",".join("?" for _ in kinds)
    rows = await db.execute_fetchall(
        f"SELECT kind FROM subagent_interactions WHERE subagent_id=? AND source_id=? AND kind IN ({placeholders})",
        (subagent_id, source_id, *(kind.value for kind in kinds)),
    )
    return SubagentInteractionKind(rows[0][0]) if rows else None
