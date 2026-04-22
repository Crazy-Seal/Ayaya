"""聊天记录存储 - 访问 chat_history 表"""

import logging
from datetime import date, datetime, time, timezone, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import aiosqlite

logger = logging.getLogger(__name__)

# 默认数据库路径
DEFAULT_DB_PATH = Path(__file__).resolve().parents[4] / "memory" / "sqlite" / "chat_history.sqlite3"


class ChatHistoryStore:
    """聊天记录存储类

    负责：
    - 保存聊天记录
    - 按日期查询消息数量
    - 获取指定日期的聊天记录
    - 获取最后一个有聊天记录的日期

    时间规则：
    - 使用配置的时区（默认系统时区）
    - 凌晨 N 点前算前一天，N 点后算当天
    - 数据库存储 UTC 时间，查询时需要转换

    有效日期 D 对应的时间范围：
    - 本地时间：D 04:00:00 ~ D+1 03:59:59（假设 day_boundary_hour=4）
    - UTC 时间：根据时区计算
    """

    TABLE_NAME = "chat_history"

    def __init__(
        self,
        db_path: Path | str | None = None,
        timezone: ZoneInfo | None = None,
        day_boundary_hour: int = 4,
    ):
        self.db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
        self.timezone = timezone or datetime.now().astimezone().tzinfo
        self.day_boundary_hour = day_boundary_hour
        self._ensure_table()

    def _ensure_table(self):
        """确保表存在"""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        import sqlite3
        with sqlite3.connect(str(self.db_path)) as conn:
            # 开启 WAL 模式，提高并发性
            conn.execute("PRAGMA journal_mode=WAL")
            # 设置等待超时（毫秒）
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute(f"""
                CREATE TABLE IF NOT EXISTS {self.TABLE_NAME} (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    thread_id TEXT NOT NULL,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL
                )
            """)
            conn.execute(f"""
                CREATE INDEX IF NOT EXISTS idx_thread_time
                ON {self.TABLE_NAME}(thread_id, timestamp)
            """)
            conn.commit()

    def _effective_date_to_utc_range(self, effective_date: date) -> tuple[datetime, datetime]:
        """将有效日期转换为 UTC 时间范围

        有效日期 D 对应：
        - 本地时间：D {day_boundary_hour}:00:00 ~ D+1 {day_boundary_hour-1}:59:59
        - UTC 时间：根据时区偏移计算

        Args:
            effective_date: 有效日期

        Returns:
            (utc_start, utc_end) UTC 时间范围
        """
        # 本地时间范围的开始和结束
        local_start = datetime.combine(
            effective_date, time(self.day_boundary_hour, 0, 0), tzinfo=self.timezone
        )
        local_end = datetime.combine(
            effective_date + timedelta(days=1), time(self.day_boundary_hour, 0, 0), tzinfo=self.timezone
        )

        # 转换为 UTC
        utc_start = local_start.astimezone(timezone.utc)
        utc_end = local_end.astimezone(timezone.utc)

        return utc_start, utc_end

    def _utc_to_effective_date(self, utc_timestamp: str | datetime | int | float) -> date:
        """将 UTC 时间戳转换为有效日期

        Args:
            utc_timestamp: UTC 时间戳（字符串、datetime、毫秒/秒时间戳）

        Returns:
            有效日期
        """
        dt = self._parse_timestamp(utc_timestamp)
        if dt is None:
            # 解析失败，尝试作为本地日期处理
            if isinstance(utc_timestamp, str):
                return date.fromisoformat(utc_timestamp)
            raise ValueError(f"无法解析时间戳: {utc_timestamp}")

        # 转换为本地时间
        local_dt = dt.astimezone(self.timezone)

        # 计算有效日期
        if local_dt.hour < self.day_boundary_hour:
            return (local_dt - timedelta(days=1)).date()
        return local_dt.date()

    def _parse_timestamp(self, timestamp: str | datetime | int | float) -> datetime | None:
        """解析各种格式的时间戳为 UTC datetime

        支持格式：
        - ISO 格式：2025-04-17T10:30:00Z 或 2025-04-17T10:30:00+00:00
        - SQL 格式：2025-04-17 10:30:00
        - 带毫秒 SQL：2025-04-17 10:30:00.123
        - 秒时间戳：1713351000（10位）
        - 毫秒时间戳：1713351000000（13位）

        Args:
            timestamp: 时间戳

        Returns:
            UTC datetime 对象，解析失败返回 None
        """
        # 处理 datetime 对象
        if isinstance(timestamp, datetime):
            if timestamp.tzinfo is None:
                return timestamp.replace(tzinfo=timezone.utc)
            return timestamp.astimezone(timezone.utc)

        # 处理数字时间戳（毫秒或秒）
        if isinstance(timestamp, (int, float)):
            ts = timestamp
            if ts > 1e12:  # 毫秒时间戳
                ts = ts / 1000
            return datetime.fromtimestamp(ts, tz=timezone.utc)

        # 处理字符串
        if isinstance(timestamp, str):
            # 尝试解析数字字符串
            try:
                ts = float(timestamp)
                if ts > 1e12:  # 毫秒时间戳
                    ts = ts / 1000
                return datetime.fromtimestamp(ts, tz=timezone.utc)
            except ValueError:
                pass

            # 尝试解析 ISO 格式
            try:
                return datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            except ValueError:
                pass

            # 尝试解析 SQL 格式（带或不带毫秒）
            try:
                dt_str = timestamp.split(".")[0]  # 去掉毫秒部分
                dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
                return dt.replace(tzinfo=timezone.utc)
            except ValueError:
                pass

        return None

    def _format_local_time(self, timestamp: str | datetime | int | float, include_weekday: bool = True) -> str:
        """将时间戳格式化为本地时间字符串

        Args:
            timestamp: 时间戳（任意格式）
            include_weekday: 是否包含星期

        Returns:
            格式化的本地时间字符串，如 "2025-04-17 周四 10:30:00" 或 "2025-04-17 10:30:00"
        """
        dt = self._parse_timestamp(timestamp)
        if dt is None:
            return str(timestamp)

        # 转换为本地时间
        local_dt = dt.astimezone(self.timezone)

        if include_weekday:
            weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
            weekday = weekdays[local_dt.weekday()]
            return f"{local_dt.strftime('%Y-%m-%d')} {weekday} {local_dt.strftime('%H:%M:%S')}"
        else:
            return local_dt.strftime('%Y-%m-%d %H:%M:%S')

    # ==================== 写入方法 ====================

    async def save_chat_message(
        self,
        session_id: str,
        role: str,
        content: str,
    ) -> None:
        """保存单条聊天记录

        Args:
            session_id: 会话ID
            role: 角色（Human/AI/Tool）
            content: 消息内容
        """
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with aiosqlite.connect(str(self.db_path)) as conn:
                await conn.execute("PRAGMA busy_timeout=5000")
                await conn.execute(
                    f"INSERT INTO {self.TABLE_NAME} (thread_id, role, content) VALUES (?, ?, ?)",
                    (session_id, role, content),
                )
                await conn.commit()
        except Exception:
            logger.exception("[ChatHistoryStore][session=%s] 保存聊天记录失败", session_id)

    # ==================== 查询方法 ====================

    async def get_message_count_by_date(
        self,
        session_id: str,
        effective_date: date,
        role: str | None = None,
    ) -> int:
        """获取指定有效日期的消息数量

        Args:
            session_id: 会话ID
            effective_date: 有效日期
            role: 角色过滤（Human/AI/Tool），None 表示不过滤

        Returns:
            消息数量
        """
        utc_start, utc_end = self._effective_date_to_utc_range(effective_date)

        async with aiosqlite.connect(str(self.db_path)) as conn:
            await conn.execute("PRAGMA busy_timeout=5000")
            if role:
                cursor = await conn.execute(
                    f"""
                    SELECT COUNT(*) FROM {self.TABLE_NAME}
                    WHERE thread_id = ? AND timestamp >= ? AND timestamp < ? AND role = ?
                    """,
                    (session_id, utc_start.isoformat(), utc_end.isoformat(), role),
                )
            else:
                cursor = await conn.execute(
                    f"""
                    SELECT COUNT(*) FROM {self.TABLE_NAME}
                    WHERE thread_id = ? AND timestamp >= ? AND timestamp < ?
                    """,
                    (session_id, utc_start.isoformat(), utc_end.isoformat()),
                )
            row = await cursor.fetchone()
            return row[0] if row else 0

    async def get_last_chat_date(
        self,
        session_id: str,
        exclude_today: date | None = None,
    ) -> date | None:
        """获取最后一个有聊天记录的有效日期

        Args:
            session_id: 会话ID
            exclude_today: 要排除的有效日期（通常是今天）

        Returns:
            最后一个有聊天记录的有效日期，如果没有则返回 None
        """
        async with aiosqlite.connect(str(self.db_path)) as conn:
            await conn.execute("PRAGMA busy_timeout=5000")
            if exclude_today:
                # 将排除的有效日期转换为 UTC 时间范围
                utc_start, utc_end = self._effective_date_to_utc_range(exclude_today)
                cursor = await conn.execute(
                    f"""
                    SELECT timestamp FROM {self.TABLE_NAME}
                    WHERE thread_id = ? AND timestamp < ?
                    ORDER BY timestamp DESC
                    LIMIT 1
                    """,
                    (session_id, utc_start.isoformat()),
                )
            else:
                cursor = await conn.execute(
                    f"""
                    SELECT timestamp FROM {self.TABLE_NAME}
                    WHERE thread_id = ?
                    ORDER BY timestamp DESC
                    LIMIT 1
                    """,
                    (session_id,),
                )
            row = await cursor.fetchone()
            if row:
                return self._utc_to_effective_date(row[0])
            return None

    async def get_messages_by_date(
        self,
        session_id: str,
        effective_date: date,
    ) -> list[dict[str, Any]]:
        """获取指定有效日期的所有聊天记录

        Args:
            session_id: 会话ID
            effective_date: 有效日期

        Returns:
            聊天记录列表，每条包含 role, content, timestamp（本地时间字符串）
        """
        utc_start, utc_end = self._effective_date_to_utc_range(effective_date)

        async with aiosqlite.connect(str(self.db_path)) as conn:
            await conn.execute("PRAGMA busy_timeout=5000")
            cursor = await conn.execute(
                f"""
                SELECT role, content, timestamp
                FROM {self.TABLE_NAME}
                WHERE thread_id = ? AND timestamp >= ? AND timestamp < ?
                ORDER BY timestamp ASC, id ASC
                """,
                (session_id, utc_start.isoformat(), utc_end.isoformat()),
            )
            rows = await cursor.fetchall()
            return [
                {
                    "role": row[0],
                    "content": row[1],
                    "timestamp": self._format_local_time(row[2]),
                }
                for row in rows
            ]

    async def list_chat_history(
        self,
        session_id: str,
        start: int = 0,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """查询会话历史

        Args:
            session_id: 会话ID
            start: 偏移量
            limit: 数量限制

        Returns:
            聊天记录列表，timestamp 为本地时间字符串
        """
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with aiosqlite.connect(str(self.db_path)) as conn:
                await conn.execute("PRAGMA busy_timeout=5000")
                cursor = await conn.execute(
                    f"""
                    SELECT role, content, timestamp
                    FROM {self.TABLE_NAME}
                    WHERE thread_id = ?
                    ORDER BY timestamp ASC, id ASC
                    LIMIT ? OFFSET ?
                    """,
                    (session_id, limit, start),
                )
                rows = await cursor.fetchall()
                return [
                    {
                        "role": row[0],
                        "content": row[1],
                        "timestamp": self._format_local_time(row[2]),
                    }
                    for row in rows
                ]
        except Exception:
            logger.exception("[ChatHistoryStore][session=%s] 查询聊天记录失败", session_id)
            return []

    async def has_chat_on_date(
        self,
        session_id: str,
        effective_date: date,
    ) -> bool:
        """检查指定有效日期是否有聊天记录

        Args:
            session_id: 会话ID
            effective_date: 有效日期

        Returns:
            是否有聊天记录
        """
        count = await self.get_message_count_by_date(session_id, effective_date)
        return count > 0

    async def get_messages_before_date(
        self,
        session_id: str,
        before_date: date,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """获取指定日期之前的最后 N 条聊天记录

        Args:
            session_id: 会话ID
            before_date: 排除的有效日期（取这天之前的）
            limit: 数量限制（默认10条，约5轮对话）

        Returns:
            聊天记录列表（按时间升序），timestamp 为本地时间字符串
        """
        # 将有效日期转换为 UTC 时间范围
        utc_start, utc_end = self._effective_date_to_utc_range(before_date)

        async with aiosqlite.connect(str(self.db_path)) as conn:
            await conn.execute("PRAGMA busy_timeout=5000")
            # 先按时间倒序取最后 N 条，再按时间正序返回
            cursor = await conn.execute(
                f"""
                SELECT role, content, timestamp FROM (
                    SELECT role, content, timestamp
                    FROM {self.TABLE_NAME}
                    WHERE thread_id = ? AND timestamp < ?
                    ORDER BY timestamp DESC
                    LIMIT ?
                ) sub
                ORDER BY timestamp ASC
                """,
                (session_id, utc_start.isoformat(), limit),
            )
            rows = await cursor.fetchall()
            return [
                {
                    "role": row[0],
                    "content": row[1],
                    "timestamp": self._format_local_time(row[2]),
                }
                for row in rows
            ]
