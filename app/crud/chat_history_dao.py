from pathlib import Path
import json
import logging
import aiosqlite
from datetime import datetime, timezone

from app.schemas.message_types import HistoryRole, LEGACY_TOOL_NOTICE_KIND
from app.agent.state.messages import (
    AssistantMessage,
    HumanMessage,
    MessageContractError,
    ToolMessage, ImageContent,
    message_from_history,
)
from app.runtime import get_chat_history_db

logger = logging.getLogger(__name__)
class ChatHistoryDao:
    """从协议日志生成桌面历史视图，保留真人和 Assistant 的入口来源。

    Attributes:
        db_path (Path | None): 共享 SQLite 路径，省略时使用当前环境的隔离存储。
    """
    READ_CHUNK_SIZE = 256

    def __init__(self, db_path: Path | None = None):
        """绑定共享协议数据库路径，构造时不读写数据库。

        Args:
            db_path (Path | None): 当前环境使用的 SQLite 数据库路径。
        """
        self.db_path = db_path or get_chat_history_db()

    @staticmethod
    def _to_local_time_text(utc_timestamp_value: object) -> str:
        """把多种 UTC 时间格式转换为系统本地时区文本。

        Args:
            utc_timestamp_value (object): 协议日志保存的 UTC 时间值。
        """
        try:
            utc_dt: datetime
            if isinstance(utc_timestamp_value, datetime):
                utc_dt = utc_timestamp_value
            elif isinstance(utc_timestamp_value, (int, float)):
                epoch = float(utc_timestamp_value)
                # 13 位时间戳通常是毫秒，先换算为秒。
                if abs(epoch) >= 1e12:
                    epoch /= 1000.0
                utc_dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
            elif isinstance(utc_timestamp_value, str):
                text = utc_timestamp_value.strip()
                if not text:
                    raise ValueError("empty timestamp string")
                try:
                    utc_dt = datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                except ValueError:
                    normalized = text.replace("Z", "+00:00")
                    try:
                        utc_dt = datetime.fromisoformat(normalized)
                    except ValueError:
                        epoch = float(text)
                        if abs(epoch) >= 1e12:
                            epoch /= 1000.0
                        utc_dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
            else:
                raise TypeError(f"unsupported timestamp type: {type(utc_timestamp_value).__name__}")

            if utc_dt.tzinfo is None:
                utc_dt = utc_dt.replace(tzinfo=timezone.utc)
            else:
                utc_dt = utc_dt.astimezone(timezone.utc)
            return utc_dt.astimezone().isoformat(timespec="seconds")
        except Exception:
            logger.warning("[ChatHistory] 无法解析 timestamp=%r", utc_timestamp_value)
            return str(utc_timestamp_value)

    @staticmethod
    def _project_protocol_row(row: tuple) -> list[dict]:
        """将一条业务记录投影为聊天历史项，分页仍在可见投影后执行。"""
        row_id, message_id, raw, created_at, kind, message_type = row
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("message_json 必须是对象")
            timestamp = ChatHistoryDao._to_local_time_text(created_at)
            base = {"id": message_id or str(row_id), "timestamp": timestamp, "images": None}
            # 1. 旧工具提示只供展示，不能伪造完整调用或进入领域解码。
            if kind == LEGACY_TOOL_NOTICE_KIND:
                return [{**base, "role": HistoryRole.AI_TOOL_CALLING, "content": str(payload.get("content", ""))}]
            message = message_from_history(message_json=payload, message_type=message_type,
                                           message_id=message_id, created_at=created_at)
        except (ValueError, TypeError) as exc:
            raise MessageContractError(f"协议消息 id={row_id} 不符合业务契约: {exc}") from exc
        images = [part.storage_ref or part.url for part in getattr(message, "content", ())
                  if isinstance(part, ImageContent) and (part.storage_ref or part.url)]
        if isinstance(message, HumanMessage):
            return [{**base, "role": HistoryRole.HUMAN, "content": message.text,
                     "images": images or None, "origin_channel": message.origin_channel}]
        # 2. 工具文字仍隐藏，图片保持独立可见气泡并使用稳定派生编号。
        if isinstance(message, ToolMessage):
            return [{**base, "id": f"{message.message_id}:images", "role": HistoryRole.AI,
                     "content": f"[系统消息]工具 {message.tool_name} 返回图片: ", "images": images}] if images else []
        if not isinstance(message, AssistantMessage):
            return []
        result = []
        if message.text:
            result.append({**base, "role": HistoryRole.AI, "content": message.text, "origin_channel": message.origin_channel})
        if message.tool_calls:
            result.append({**base, "id": f"{message.message_id}:tools", "role": HistoryRole.AI_TOOL_CALLING,
                "content": "调用了工具: " + ", ".join(call.name for call in message.tool_calls), "origin_channel": message.origin_channel})
        return result


    @staticmethod
    async def _protocol_table_exists(conn: aiosqlite.Connection) -> bool:
        """检查协议日志表是否存在，让新环境的历史查询返回空结果。

        Args:
            conn (aiosqlite.Connection): 本次查询使用的 SQLite 连接。

        Returns:
            bool: conversation_messages 表存在时为 True。
        """
        rows = await conn.execute_fetchall(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='conversation_messages'"
        )
        return bool(rows)

    @staticmethod
    async def _fetch_protocol_chunk(
        conn: aiosqlite.Connection,
        session_id: str,
        *,
        after_id: int | None = None,
        before_id: int | None = None,
        descending: bool = False,
        limit: int,
    ) -> list[tuple]:
        """按会话和数据库编号范围查询一块协议记录，可见消息的筛选由调用方继续处理。

        Args:
            conn (aiosqlite.Connection): 本次查询使用的 SQLite 连接。
            session_id (str): 当前操作所属的会话编号。
            after_id (int | None): 只查询数据库编号大于该值的协议行。
            before_id (int | None): 只查询数据库编号小于该值的协议行。
            descending (bool): 是否从最新记录向前查询。
            limit (int): 最多返回的记录数量。

        Returns:
            list[tuple]: 按指定方向排列的协议行。
        """
        clauses = ["session_id = ?"]
        params: list[object] = [session_id]
        if after_id is not None:
            clauses.append("id > ?")
            params.append(after_id)
        if before_id is not None:
            clauses.append("id < ?")
            params.append(before_id)
        params.append(limit)
        direction = "DESC" if descending else "ASC"
        return await conn.execute_fetchall(
            f"""
            SELECT id, message_id, message_json, created_at, record_kind, message_type
            FROM conversation_messages
            WHERE {' AND '.join(clauses)}
            ORDER BY id {direction}
            LIMIT ?
            """,
            params,
        )

    async def list_chat_history_async(self, session_id: str, start: int = 0, limit: int = 200) -> list[dict[str, str]]:
        """从协议日志投影可见历史，再执行 UI 分页。

        Args:
            session_id (str): 当前操作所属的会话编号。
            start (int): 查询范围的起始日期或时间。
            limit (int): 最多返回的记录数量。
        """
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        start = max(start, 0)
        limit = max(limit, 0)
        if limit == 0:
            return []
        try:
            result: list[dict[str, str]] = []
            visible_index = 0
            after_id: int | None = None
            async with aiosqlite.connect(str(self.db_path)) as conn:
                if not await self._protocol_table_exists(conn):
                    return []
                while len(result) < limit:
                    rows = await self._fetch_protocol_chunk(
                        conn,
                        session_id,
                        after_id=after_id,
                        limit=self.READ_CHUNK_SIZE,
                    )
                    if not rows:
                        break
                    after_id = int(rows[-1][0])
                    for row in rows:
                        for item in self._project_protocol_row(row):
                            if visible_index >= start and len(result) < limit:
                                result.append(item)
                            visible_index += 1
                    if len(rows) < self.READ_CHUNK_SIZE:
                        break
            return result
        except MessageContractError:
            logger.exception("[ChatHistory][session=%s] 消息契约不兼容", session_id)
            raise
        except Exception:
            logger.exception("[ChatHistory][session=%s] 查询聊天记录失败", session_id)
            return []

    async def list_chat_history_last_n_async(self, session_id: str, n: int = 100) -> list[dict[str, str]]:
        """查询会话最后 N 条历史记录，按时间升序返回。

        Args:
            session_id (str): 当前操作所属的会话编号。
            n (int): 最多返回的日记条数。
        """
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        n = max(n, 0)
        if n == 0:
            return []
        try:
            projected_groups: list[list[dict[str, str]]] = []
            visible_count = 0
            before_id: int | None = None
            async with aiosqlite.connect(str(self.db_path)) as conn:
                if not await self._protocol_table_exists(conn):
                    return []
                while visible_count < n:
                    rows = await self._fetch_protocol_chunk(
                        conn,
                        session_id,
                        before_id=before_id,
                        descending=True,
                        limit=self.READ_CHUNK_SIZE,
                    )
                    if not rows:
                        break
                    before_id = int(rows[-1][0])
                    for row in rows:
                        group = self._project_protocol_row(row)
                        projected_groups.append(group)
                        visible_count += len(group)
                    if len(rows) < self.READ_CHUNK_SIZE:
                        break
            ordered = [
                item
                for group in reversed(projected_groups)
                for item in group
            ]
            return ordered[-n:]
        except MessageContractError:
            logger.exception("[ChatHistory][session=%s] 消息契约不兼容", session_id)
            raise
        except Exception:
            logger.exception("[ChatHistory][session=%s] 查询最后聊天记录失败", session_id)
            return []
