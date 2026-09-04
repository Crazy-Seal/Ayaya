from pathlib import Path
import json
import logging
import aiosqlite
from datetime import datetime, timezone

from app.runtime import get_chat_history_db

logger = logging.getLogger(__name__)
class ChatHistoryDao:
    def __init__(self, db_path: Path | None = None):
        self.db_path = db_path or get_chat_history_db()

    @staticmethod
    def _to_local_time_text(utc_timestamp_value: object) -> str:
        """把多种 UTC 时间格式转换为系统本地时区文本。"""
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
        row_id, message_id, role, raw_json, created_at, record_kind, attachments_json = row
        try:
            message = json.loads(raw_json)
        except (TypeError, json.JSONDecodeError):
            logger.warning("[ChatHistory] 无法解析协议消息 id=%s", row_id)
            return []
        content_value = message.get("content", "")
        if isinstance(content_value, list):
            content = "\n".join(
                str(part.get("text", ""))
                for part in content_value
                if isinstance(part, dict) and part.get("type") == "text"
            )
        else:
            content = str(content_value or "")
        timestamp = ChatHistoryDao._to_local_time_text(created_at)
        images = json.loads(attachments_json) if attachments_json else None
        base = {"id": message_id or str(row_id), "timestamp": timestamp, "images": images}
        if record_kind == "legacy_tool_notice":
            return [{**base, "role": "AI_Tool_Calling", "content": content}]
        if record_kind == "tool_image":
            image_urls = [
                part.get("image_url", {}).get("url")
                for part in content_value if isinstance(content_value, list)
                if isinstance(part, dict) and part.get("type") == "image_url"
            ]
            return [{**base, "role": "AI", "content": content, "images": images or image_urls}]
        if role == "user":
            return [{**base, "role": "Human", "content": content}]
        if role == "tool":
            return []
        projected: list[dict] = []
        if content:
            projected.append({**base, "role": "AI", "content": content})
        tool_calls = message.get("tool_calls") or []
        if tool_calls:
            names = [
                str((tool_call.get("function") or {}).get("name") or "未知工具")
                for tool_call in tool_calls
            ]
            projected.append({
                **base,
                "id": f"{base['id']}:tools",
                "role": "AI_Tool_Calling",
                "content": f"调用了工具: {', '.join(names)}",
            })
        return projected

    async def _visible_protocol_rows(self, session_id: str) -> list[dict]:
        async with aiosqlite.connect(str(self.db_path)) as conn:
            exists = await conn.execute_fetchall(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='conversation_messages'"
            )
            rows = await conn.execute_fetchall(
                """
                SELECT id, message_id, role, message_json, created_at,
                       record_kind, attachments_json
                FROM conversation_messages
                WHERE session_id = ?
                ORDER BY id ASC
                """,
                (session_id,),
            ) if exists else []
        return [item for row in rows for item in self._project_protocol_row(row)]

    async def list_chat_history_async(self, session_id: str, start: int = 0, limit: int = 200) -> list[dict[str, str]]:
        """从协议日志投影可见历史，再执行 UI 分页。"""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            rows = await self._visible_protocol_rows(session_id)
            return rows[max(start, 0) : max(start, 0) + max(limit, 0)]
        except Exception:
            logger.exception("[ChatHistory][session=%s] 查询聊天记录失败", session_id)
            return []

    async def list_chat_history_last_n_async(self, session_id: str, n: int = 100) -> list[dict[str, str]]:
        """查询会话最后 N 条历史记录，按时间升序返回。"""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            rows = await self._visible_protocol_rows(session_id)
            return rows[-max(n, 0) :] if n else []
        except Exception:
            logger.exception("[ChatHistory][session=%s] 查询最后聊天记录失败", session_id)
            return []
