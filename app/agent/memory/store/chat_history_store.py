"""面向日记和长期记忆的 OpenAI 协议日志只读查询。"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timezone, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import aiosqlite

from app.agent.state.messages import (
    AssistantMessage,
    BaseMessage,
    HumanMessage,
    message_from_protocol_format,
)
from app.runtime import get_chat_history_db


class ChatHistoryStore:
    """为记忆提取只读查询共享对话协议日志，不拥有建表或写入消息的职责。"""
    TABLE_NAME = "conversation_messages"
    READ_CHUNK_SIZE = 256

    def __init__(
        self,
        db_path: Path | str | None = None,
        timezone: ZoneInfo | None = None,
        day_boundary_hour: int = 4,
    ) -> None:
        """保存协议日志路径、时区和日期分界配置；构造时不连接数据库或初始化表。

        Args:
            db_path (Path | str | None): 当前环境使用的 SQLite 数据库路径。
            timezone (ZoneInfo | None): 日记日期计算使用的时区；省略时使用本机时区。
            day_boundary_hour (int): 本地一天的起始小时，默认凌晨四点。
        """
        self.db_path = Path(db_path) if db_path else get_chat_history_db()
        self.timezone = timezone or datetime.now().astimezone().tzinfo
        self.day_boundary_hour = day_boundary_hour

    def _local_date_to_utc_range(self, local_date: date) -> tuple[datetime, datetime]:
        """把日记有效日期换成 UTC 查询边界，本地凌晨分界之前的对话仍属于前一天。

        Args:
            local_date (date): 按记忆配置划分的本地有效日期。

        Returns:
            tuple[datetime, datetime]: 包含起点、不包含终点的 UTC 时间范围。
        """
        start = datetime.combine(local_date, time(self.day_boundary_hour), tzinfo=self.timezone)
        end = datetime.combine(local_date + timedelta(days=1), time(self.day_boundary_hour), tzinfo=self.timezone)
        return start.astimezone(timezone.utc), end.astimezone(timezone.utc)

    def _parse_timestamp(self, value: str | datetime | int | float) -> datetime | None:
        """兼容 ISO 时间、秒和毫秒时间戳，将无时区的历史值按 UTC 解释。

        Args:
            value (str | datetime | int | float): datetime、ISO 字符串或秒/毫秒时间戳。

        Returns:
            datetime | None: 统一为 UTC 的时间，无法识别的值返回 None。
        """
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
        """将历史消息时间转换为记忆时区下的有效日期，应用凌晨分界规则。

        Args:
            value (str | datetime | int | float): 当前需要校验或转换的值。

        Returns:
            date: 用于日记生成和查询的有效日期。

        Raises:
            ValueError: 时间戳和日期字符串均无法解析。
        """
        parsed = self._parse_timestamp(value)
        if parsed is None:
            if isinstance(value, str):
                return date.fromisoformat(value)
            raise ValueError(f"无法解析时间戳: {value}")
        local = parsed.astimezone(self.timezone)
        return (local - timedelta(hours=self.day_boundary_hour)).date()

    def _format_local_time(self, value: str | datetime | int | float, include_weekday: bool = True) -> str:
        """按记忆配置时区展示历史消息时间，无法解析的旧值保留原表示。

        Args:
            value (str | datetime | int | float): 当前需要校验或转换的值。
            include_weekday (bool): 时间展示中是否包含中文星期。

        Returns:
            str: 本地日期时间及可选星期文本。
        """
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
        """按会话、时间范围及已捕获日志边界读取协议行，排除旧工具提示记录。

        Args:
            session_id (str): 当前操作所属的会话编号。
            max_history_id (int | None): 本次提取开始前捕获的协议日志最大编号；排除之后到达的消息。
            start (datetime | None): 查询范围的起始日期或时间。
            end (datetime | None): 查询范围的结束日期或时间。

        Returns:
            list[tuple]: 按数据库编号递增的协议行；协议表尚不存在时返回空列表。
        """
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
                SELECT id, message_id, created_at, role, message_json, record_kind,
                       message_type, domain_json, associated_message_id
                FROM {self.TABLE_NAME}
                WHERE {' AND '.join(clauses)} AND record_kind <> 'legacy_tool_notice'
                ORDER BY id ASC
                """,
                params,
            )

    async def _table_exists(self, conn: aiosqlite.Connection) -> bool:
        """读取 SQLite 表目录，避免只读记忆查询在新数据库中误建业务表。

        Args:
            conn (aiosqlite.Connection): 本次查询使用的 SQLite 连接。

        Returns:
            bool: 对话协议日志表已经存在时为 True。
        """
        row = await conn.execute_fetchall(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (self.TABLE_NAME,),
        )
        return bool(row)

    @staticmethod
    def _decode(row: tuple) -> BaseMessage:
        """将协议正文、消息身份和独立领域字段恢复为强类型消息。

        Args:
            row (tuple): 从数据库读取的单条协议记录。

        Returns:
            BaseMessage: 保留原编号、时间、来源和媒体关联的领域消息。
        """
        (
            _row_id,
            message_id,
            created_at,
            _role,
            raw,
            _record_kind,
            message_type,
            raw_domain,
            associated_message_id,
        ) = row
        return message_from_protocol_format(
            message_type=message_type,
            message_json=json.loads(raw),
            message_id=str(message_id),
            created_at=created_at,
            domain_json=json.loads(raw_domain) if raw_domain else {},
            associated_message_id=associated_message_id,
        )

    async def get_latest_message_id(self, session_id: str) -> int:
        """捕获本会话协议日志的最大数据库编号，供本次记忆提取排除后来到达的消息。

        Args:
            session_id (str): 当前操作所属的会话编号。

        Returns:
            int: 最新协议行编号；无记录或尚未建表时为零。
        """
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
        """从最新协议行向前找符合日期及日志边界条件的最近聊天日。

        Args:
            session_id (str): 当前操作所属的会话编号。
            exclude_today (date | None): 仅返回早于此有效日期的聊天日期；为空时不排除当前日期。
            max_history_id (int | None): 本次提取开始前捕获的协议日志最大编号；排除之后到达的消息。

        Returns:
            date | None: 符合条件的有效日期；没有记录时为空。
        """
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
                WHERE {' AND '.join(clauses)} AND record_kind <> 'legacy_tool_notice'
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
    ) -> list[BaseMessage]:
        """读取指定有效日期内、且不超过本次提取边界的全部协议消息。

        Args:
            session_id (str): 当前操作所属的会话编号。
            effective_date (date): 按日记规则划分的有效日期，格式为 YYYY-MM-DD。
            max_history_id (int | None): 本次提取开始前捕获的协议日志最大编号；排除之后到达的消息。

        Returns:
            list[BaseMessage]: 按数据库编号排列的领域消息。
        """
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
    ) -> list[BaseMessage]:
        """只查询目标日期之前最近的指定条数，作为生成日记的前情材料。

        Args:
            session_id (str): 当前操作所属的会话编号。
            before_date (date): 只读取该有效日期之前的记录。
            limit (int): 最多返回的记录数量。
            max_history_id (int | None): 本次提取开始前捕获的协议日志最大编号；排除之后到达的消息。

        Returns:
            list[BaseMessage]: 最近记录按原对话顺序排列的消息列表。
        """
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
                SELECT id, message_id, created_at, role, message_json, record_kind,
                       message_type, domain_json, associated_message_id
                FROM {self.TABLE_NAME}
                WHERE {' AND '.join(clauses)} AND record_kind <> 'legacy_tool_notice'
                ORDER BY id DESC
                LIMIT ?
                """,
                params,
            )
        return [self._decode(row) for row in reversed(rows)]

    @staticmethod
    def _completed_turns(records: list[BaseMessage]) -> list[list[BaseMessage]]:
        """按真人输入划分历史轮次，仅保留出现最终普通 assistant 回复的轮次。

        Args:
            records (list[BaseMessage]): 按原对话顺序排列的领域消息。

        Returns:
            list[list[BaseMessage]]: 具备最终回复的轮次列表；只有工具请求而无最终回复的轮次被排除。
        """
        turns: list[list[BaseMessage]] = []
        active: list[BaseMessage] = []
        for message in records:
            if isinstance(message, HumanMessage):
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
                isinstance(message, AssistantMessage)
                and not message.tool_calls
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
    ) -> tuple[list[BaseMessage], list[BaseMessage]]:
        """从数据库分块向前查询，收集足够完整轮次后分成提取批次与更早前情。

        Args:
            session_id (str): 当前操作所属的会话编号。
            current_turns (int): 本次提取所需的最近完整轮次数。
            history_turns (int): 仅作为前情提供的更早完整轮次数。
            max_history_id (int | None): 本次提取开始前捕获的协议日志最大编号；排除之后到达的消息。

        Returns:
            tuple[list[BaseMessage], list[BaseMessage]]: 前情消息列表和当前提取批次列表，均保持原对话顺序。
        """
        current_turns = max(current_turns, 0)
        history_turns = max(history_turns, 0)
        required = current_turns + history_turns
        if required == 0:
            return [], []

        # 1. 从最新协议行分块向前读取，受本次捕获的最大编号限制，不加载会话全部历史。
        records: list[BaseMessage] = []
        complete: list[list[BaseMessage]] = []
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
                    SELECT id, message_id, created_at, role, message_json, record_kind,
                           message_type, domain_json, associated_message_id
                    FROM {self.TABLE_NAME}
                    WHERE {' AND '.join(clauses)} AND record_kind <> 'legacy_tool_notice'
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    params,
                )
                if not rows:
                    break
                # 2. 新读到的旧行放到前面，再按完整真人轮次判断；块开头只有回复的片段不能算一轮。
                records = [self._decode(row) for row in reversed(rows)] + records
                complete = self._completed_turns(records)
                before_id = int(rows[-1][0])
                if len(rows) < self.READ_CHUNK_SIZE:
                    break

        # 3. 最近完整轮次作为提取批次，更早几轮仅提供前情，两部分都保持原消息顺序。
        selected = complete[-current_turns:] if current_turns else []
        history_end = len(complete) - len(selected)
        history_start = max(history_end - history_turns, 0)
        history = complete[history_start:history_end]
        return (
            [message for turn in history for message in turn],
            [message for turn in selected for message in turn],
        )
