from pathlib import Path
import json
import logging
import aiosqlite
from datetime import datetime, timezone

from app.agent.state.messages import (
    AssistantMessage,
    HumanMessage,
    MessageContractError,
    ToolMediaMessage,
    message_from_protocol_format,
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
        """恢复协议行并生成可见历史项，真人和 Assistant 项携带入口来源。

        Args:
            row (tuple): 固定查询列顺序的 SQLite 行。

        Returns:
            list[dict]: 一条协议行对应的零个或多个可见历史项。

        Raises:
            MessageContractError: 输入、持久状态或系统调用不满足当前执行约束。
        """
        (
            row_id,
            message_id,
            role,
            raw_json,
            created_at,
            record_kind,
            attachments_json,
            message_type,
            raw_domain,
            associated_message_id,
        ) = row
        try:
            message = json.loads(raw_json)
        except (TypeError, json.JSONDecodeError) as exc:
            raise MessageContractError(
                f"协议消息 id={row_id} 的 message_json 无效"
            ) from exc
        if not isinstance(message, dict):
            raise MessageContractError(
                f"协议消息 id={row_id} 的 message_json 必须是对象"
            )
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
        try:
            images = json.loads(attachments_json) if attachments_json else None
        except (TypeError, json.JSONDecodeError) as exc:
            raise MessageContractError(
                f"协议消息 id={row_id} 的 attachments_json 无效"
            ) from exc
        base = {"id": message_id or str(row_id), "timestamp": timestamp, "images": images}
        if record_kind == "legacy_tool_notice":
            return [{**base, "role": "AI_Tool_Calling", "content": content}]
        if not message_type:
            raise MessageContractError(
                f"协议消息 id={row_id} 缺少 message_type；数据库与当前消息契约不兼容"
            )
        try:
            domain_message = message_from_protocol_format(
                message_type=str(message_type),
                message_json=message,
                message_id=str(message_id),
                created_at=created_at,
                domain_json=json.loads(raw_domain) if raw_domain else {},
                associated_message_id=associated_message_id,
            )
        except MessageContractError:
            raise
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise MessageContractError(
                f"协议消息 id={row_id} 不符合当前消息契约"
            ) from exc
        if isinstance(domain_message, ToolMediaMessage):
            image_urls = [
                part.get("image_url", {}).get("url")
                for part in content_value if isinstance(content_value, list)
                if isinstance(part, dict) and part.get("type") == "image_url"
            ]
            return [{**base, "role": "AI", "content": content, "images": images or image_urls}]
        if isinstance(domain_message, HumanMessage):
            return [{**base, "role": "Human", "content": domain_message.text,
                     "origin_channel": domain_message.origin_channel}]
        if not isinstance(domain_message, AssistantMessage):
            return []
        projected: list[dict] = []
        if domain_message.text:
            projected.append({**base, "role": "AI", "content": domain_message.text,
                              "origin_channel": domain_message.origin_channel})
        if domain_message.tool_calls:
            names = [call.name for call in domain_message.tool_calls]
            projected.append({
                **base,
                "id": f"{base['id']}:tools",
                "role": "AI_Tool_Calling",
                "content": f"调用了工具: {', '.join(names)}",
                "origin_channel": domain_message.origin_channel,
            })
        return projected

    @staticmethod
    async def _protocol_table_exists(conn: aiosqlite.Connection) -> bool:
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
            SELECT id, message_id, role, message_json, created_at,
                   record_kind, attachments_json, message_type, domain_json,
                   associated_message_id
            FROM conversation_messages
            WHERE {' AND '.join(clauses)}
            ORDER BY id {direction}
            LIMIT ?
            """,
            params,
        )

    async def list_chat_history_async(self, session_id: str, start: int = 0, limit: int = 200) -> list[dict[str, str]]:
        """从协议日志投影可见历史，再执行 UI 分页。"""
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
        """查询会话最后 N 条历史记录，按时间升序返回。"""
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
