"""面向日记和长期记忆的 OpenAI 协议日志只读查询。"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timezone, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import aiosqlite

from app.agent.state.messages import is_real_human_message
from app.runtime import get_chat_history_db


class ChatHistoryStore:
    TABLE_NAME = "conversation_messages"
    READ_CHUNK_SIZE = 256

    def __init__(
        self,
        db_path: Path | str | None = None,
        timezone: ZoneInfo | None = None,
        day_boundary_hour: int = 4,
    ) -> None:
        self.db_path = Path(db_path) if db_path else get_chat_history_db()
        self.timezone = timezone or datetime.now().astimezone().tzinfo
        self.day_boundary_hour = day_boundary_hour

    def _local_date_to_utc_range(self, local_date: date) -> tuple[datetime, datetime]:
        start = datetime.combine(local_date, time(self.day_boundary_hour), tzinfo=self.timezone)
        end = datetime.combine(local_date + timedelta(days=1), time(self.day_boundary_hour), tzinfo=self.timezone)
        return start.astimezone(timezone.utc), end.astimezone(timezone.utc)

    def _parse_timestamp(self, value: str | datetime | int | float) -> datetime | None:
        if isinstance(value, datetime):
            return value.replace(tzinfo=value.tzinfo or timezone.utc).astimezone(timezone.utc)
        if isinstance(value, (int, float)):
            value = value / 1000 if abs(value) >= 1e12 else value
            return datetime.fromtimestamp(value, tz=timezone.utc)
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(
                    tzinfo=datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo or timezone.utc
                ).astimezone(timezone.utc)
            except ValueError:
                try:
                    number = float(value)
                    number = number / 1000 if abs(number) >= 1e12 else number
                    return datetime.fromtimestamp(number, tz=timezone.utc)
                except ValueError:
                    return None
        return None

    def _utc_to_local_date(self, value: str | datetime | int | float) -> date:
        parsed = self._parse_timestamp(value)
        if parsed is None:
            if isinstance(value, str):
                return date.fromisoformat(value)
            raise ValueError(f"无法解析时间戳: {value}")
        local = parsed.astimezone(self.timezone)
        return (local - timedelta(hours=self.day_boundary_hour)).date()

    def _format_local_time(self, value: str | datetime | int | float, include_weekday: bool = True) -> str:
        parsed = self._parse_timestamp(value)
        if parsed is None:
            return str(value)
        local = parsed.astimezone(self.timezone)
        if not include_weekday:
            return local.strftime("%Y-%m-%d %H:%M:%S")
        weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        return f"{local:%Y-%m-%d} {weekdays[local.weekday()]} {local:%H:%M:%S}"

    async def _rows(
        self,
        session_id: str,
        *,
        max_history_id: int | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[tuple]:
        clauses = ["session_id = ?"]
        params: list[Any] = [session_id]
        if max_history_id is not None:
            clauses.append("id <= ?")
            params.append(max_history_id)
        if start is not None:
            clauses.append("created_at >= ?")
            params.append(start.isoformat())
        if end is not None:
            clauses.append("created_at < ?")
            params.append(end.isoformat())
        async with aiosqlite.connect(str(self.db_path)) as conn:
            await conn.execute("PRAGMA busy_timeout=5000")
            if not await self._table_exists(conn):
                return []
            return await conn.execute_fetchall(
                f"""
                SELECT id, message_id, created_at, role, message_json, record_kind
                FROM {self.TABLE_NAME}
                WHERE {' AND '.join(clauses)}
                ORDER BY id ASC
                """,
                params,
            )

    async def _table_exists(self, conn: aiosqlite.Connection) -> bool:
        row = await conn.execute_fetchall(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (self.TABLE_NAME,),
        )
        return bool(row)

    @staticmethod
    def _decode(row: tuple) -> dict[str, Any]:
        row_id, message_id, created_at, role, raw, record_kind = row
        message = json.loads(raw)
        message["_history_id"] = row_id
        message["_message_id"] = message_id
        message["_created_at"] = created_at
        message["_record_kind"] = record_kind
        message["role"] = role
        return message

    async def get_latest_message_id(self, session_id: str) -> int:
        async with aiosqlite.connect(str(self.db_path)) as conn:
            await conn.execute("PRAGMA busy_timeout=5000")
            if not await self._table_exists(conn):
                return 0
            rows = await conn.execute_fetchall(
                f"SELECT COALESCE(MAX(id), 0) FROM {self.TABLE_NAME} WHERE session_id = ?",
                (session_id,),
            )
        return int(rows[0][0])

    async def get_last_chat_date(
        self,
        session_id: str,
        exclude_today: date | None = None,
        max_history_id: int | None = None,
    ) -> date | None:
        clauses = ["session_id = ?"]
        params: list[Any] = [session_id]
        if max_history_id is not None:
            clauses.append("id <= ?")
            params.append(max_history_id)
        async with aiosqlite.connect(str(self.db_path)) as conn:
            await conn.execute("PRAGMA busy_timeout=5000")
            if not await self._table_exists(conn):
                return None
            async with conn.execute(
                f"""
                SELECT created_at
                FROM {self.TABLE_NAME}
                WHERE {' AND '.join(clauses)}
                ORDER BY id DESC
                """,
                params,
            ) as cursor:
                async for row in cursor:
                    effective = self._utc_to_local_date(row[0])
                    if exclude_today is None or effective < exclude_today:
                        return effective
        return None

    async def get_messages_by_date(
        self,
        session_id: str,
        effective_date: date,
        max_history_id: int | None = None,
    ) -> list[dict[str, Any]]:
        start, end = self._local_date_to_utc_range(effective_date)
        return [
            self._decode(row)
            for row in await self._rows(
                session_id, max_history_id=max_history_id, start=start, end=end
            )
        ]

    async def get_messages_before_date(
        self,
        session_id: str,
        before_date: date,
        limit: int = 10,
        *,
        max_history_id: int | None = None,
    ) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        start, _ = self._local_date_to_utc_range(before_date)
        clauses = ["session_id = ?", "created_at < ?"]
        params: list[Any] = [session_id, start.isoformat()]
        if max_history_id is not None:
            clauses.append("id <= ?")
            params.append(max_history_id)
        params.append(limit)
        async with aiosqlite.connect(str(self.db_path)) as conn:
            await conn.execute("PRAGMA busy_timeout=5000")
            if not await self._table_exists(conn):
                return []
            rows = await conn.execute_fetchall(
                f"""
                SELECT id, message_id, created_at, role, message_json, record_kind
                FROM {self.TABLE_NAME}
                WHERE {' AND '.join(clauses)}
                ORDER BY id DESC
                LIMIT ?
                """,
                params,
            )
        return [self._decode(row) for row in reversed(rows)]

    @staticmethod
    def _completed_turns(records: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
        turns: list[list[dict[str, Any]]] = []
        active: list[dict[str, Any]] = []
        for message in records:
            if is_real_human_message(message):
                if active:
                    turns.append(active)
                active = [message]
            elif active:
                active.append(message)
        if active:
            turns.append(active)
        return [
            turn for turn in turns
            if any(
                message.get("role") == "assistant"
                and not message.get("tool_calls")
                for message in turn[1:]
            )
        ]

    async def get_recent_completed_turns(
        self,
        session_id: str,
        *,
        current_turns: int = 10,
        history_turns: int = 5,
        max_history_id: int | None = None,
    ) -> tuple[list[dict], list[dict]]:
        """返回前情和当前批次；只接受具有最终普通 assistant 的完整轮次。"""
        current_turns = max(current_turns, 0)
        history_turns = max(history_turns, 0)
        required = current_turns + history_turns
        if required == 0:
            return [], []

        records: list[dict[str, Any]] = []
        complete: list[list[dict[str, Any]]] = []
        before_id: int | None = None
        async with aiosqlite.connect(str(self.db_path)) as conn:
            await conn.execute("PRAGMA busy_timeout=5000")
            if not await self._table_exists(conn):
                return [], []
            while len(complete) < required:
                clauses = ["session_id = ?"]
                params: list[Any] = [session_id]
                if before_id is not None:
                    clauses.append("id < ?")
                    params.append(before_id)
                elif max_history_id is not None:
                    clauses.append("id <= ?")
                    params.append(max_history_id)
                params.append(self.READ_CHUNK_SIZE)
                rows = await conn.execute_fetchall(
                    f"""
                    SELECT id, message_id, created_at, role, message_json, record_kind
                    FROM {self.TABLE_NAME}
                    WHERE {' AND '.join(clauses)}
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    params,
                )
                if not rows:
                    break
                records = [self._decode(row) for row in reversed(rows)] + records
                complete = self._completed_turns(records)
                before_id = int(rows[-1][0])
                if len(rows) < self.READ_CHUNK_SIZE:
                    break

        selected = complete[-current_turns:] if current_turns else []
        history_end = len(complete) - len(selected)
        history_start = max(history_end - history_turns, 0)
        history = complete[history_start:history_end]
        return (
            [message for turn in history for message in turn],
            [message for turn in selected for message in turn],
        )
