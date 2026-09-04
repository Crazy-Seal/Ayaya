"""面向日记和长期记忆的 OpenAI 协议日志只读查询。"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timezone, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import aiosqlite

from app.agent.message import is_real_human_message
from app.runtime import get_chat_history_db


class ChatHistoryStore:
    TABLE_NAME = "conversation_messages"

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
            exists = await conn.execute_fetchall(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (self.TABLE_NAME,)
            )
            if not exists:
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
        rows = await self._rows(session_id)
        return int(rows[-1][0]) if rows else 0

    async def get_message_count_by_date(
        self,
        session_id: str,
        effective_date: date,
        role: str | None = None,
        max_history_id: int | None = None,
    ) -> int:
        messages = await self.get_messages_by_date(session_id, effective_date, max_history_id)
        if role in {"Human", "user"}:
            return sum(1 for message in messages if is_real_human_message(message))
        if role:
            normalized = {"AI": "assistant", "Tool": "tool"}.get(role, role)
            return sum(1 for message in messages if message.get("role") == normalized)
        return len(messages)

    async def get_last_chat_date(
        self,
        session_id: str,
        exclude_today: date | None = None,
        max_history_id: int | None = None,
    ) -> date | None:
        rows = await self._rows(session_id, max_history_id=max_history_id)
        for row in reversed(rows):
            effective = self._utc_to_local_date(row[2])
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
        start, _ = self._local_date_to_utc_range(before_date)
        rows = await self._rows(
            session_id,
            max_history_id=max_history_id,
            end=start,
        )
        return [self._decode(row) for row in rows[-limit:]]

    async def get_recent_completed_turns(
        self,
        session_id: str,
        *,
        current_turns: int = 10,
        history_turns: int = 5,
        max_history_id: int | None = None,
    ) -> tuple[list[dict], list[dict]]:
        """返回前情和当前批次；只接受具有最终普通 assistant 的完整轮次。"""
        records = [
            self._decode(row)
            for row in await self._rows(session_id, max_history_id=max_history_id)
        ]
        turns: list[list[dict]] = []
        active: list[dict] = []
        for message in records:
            if is_real_human_message(message):
                if active:
                    turns.append(active)
                active = [message]
            elif active:
                active.append(message)
        if active:
            turns.append(active)
        complete = [
            turn for turn in turns
            if any(
                message.get("role") == "assistant"
                and not message.get("tool_calls")
                for message in turn[1:]
            )
        ]
        selected = complete[-current_turns:]
        history = complete[-(current_turns + history_turns):-current_turns]
        return (
            [message for turn in history for message in turn],
            [message for turn in selected for message in turn],
        )
