from pathlib import Path
import asyncio
import sqlite3

import aiosqlite
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver


CHECKPOINT_DB_PATH = Path(__file__).resolve().parents[2] / "memory" / "sqlite" / "checkpoints.sqlite3"


_CHECKPOINTER: AsyncSqliteSaver | None = None
_CHECKPOINTER_LOCK = asyncio.Lock()


async def get_checkpointer_async() -> AsyncSqliteSaver:
    """返回进程级缓存的 AsyncSqliteSaver，用于异步 LangGraph checkpoint 持久化。"""
    global _CHECKPOINTER
    if _CHECKPOINTER is not None:
        return _CHECKPOINTER

    async with _CHECKPOINTER_LOCK:
        if _CHECKPOINTER is not None:
            return _CHECKPOINTER

        CHECKPOINT_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(str(CHECKPOINT_DB_PATH))
        saver = AsyncSqliteSaver(conn)
        await saver.setup()
        _CHECKPOINTER = saver
    return _CHECKPOINTER


class CheckpointRepository:
    def __init__(self, db_path: Path = CHECKPOINT_DB_PATH):
        self.db_path = db_path

    def get_thread_checkpoint_watermark(self, thread_id: str, checkpoint_ns: str = "") -> int:
        # 水位线表示“本轮开始前的最后一条 checkpoint rowid”。
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(self.db_path)) as conn:
            row = conn.execute(
                """
                SELECT COALESCE(MAX(rowid), 0)
                FROM checkpoints
                WHERE thread_id = ? AND checkpoint_ns = ?
                """,
                (thread_id, checkpoint_ns),
            ).fetchone()
        return int(row[0]) if row else 0

    def rollback_thread_checkpoints(
        self,
        thread_id: str,
        baseline_rowid: int,
        checkpoint_ns: str = "",
    ) -> tuple[int, int]:
        # 回滚策略：先删 writes，再删 checkpoints，避免悬挂引用。
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(self.db_path)) as conn:
            checkpoint_ids = [
                row[0]
                for row in conn.execute(
                    """
                    SELECT checkpoint_id
                    FROM checkpoints
                    WHERE thread_id = ? AND checkpoint_ns = ? AND rowid > ?
                    """,
                    (thread_id, checkpoint_ns, baseline_rowid),
                ).fetchall()
            ]

            deleted_writes = 0
            if checkpoint_ids:
                placeholders = ",".join("?" for _ in checkpoint_ids)
                params = [thread_id, checkpoint_ns, *checkpoint_ids]
                write_cursor = conn.execute(
                    f"""
                    DELETE FROM writes
                    WHERE thread_id = ?
                      AND checkpoint_ns = ?
                      AND checkpoint_id IN ({placeholders})
                    """,
                    params,
                )
                deleted_writes = int(write_cursor.rowcount)

            checkpoint_cursor = conn.execute(
                """
                DELETE FROM checkpoints
                WHERE thread_id = ? AND checkpoint_ns = ? AND rowid > ?
                """,
                (thread_id, checkpoint_ns, baseline_rowid),
            )
            deleted_checkpoints = int(checkpoint_cursor.rowcount)
            conn.commit()

        return deleted_checkpoints, deleted_writes

