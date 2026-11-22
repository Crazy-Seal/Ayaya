"""一次性把 v2 checkpoint 和模型格式历史迁移为 Ayaya 业务协议。

检查只读；应用迁移必须停机，在同一 SQLite 事务中更新全部数据。图片文件、
记忆存储和设置不参与迁移。旧解码规则仅在本工具中存在。
"""

from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent.state.messages import (
    ImageContent, TextContent, ToolCall, ProviderData, PROTOCOL_MESSAGE_TYPES,
    message_from_storage_format, notification_text,
)
from scripts.legacy_checkpoint_validation import validate_legacy_checkpoint


class MigrationError(ValueError):
    """旧数据缺少明确恢复材料，事务必须回滚。"""


def convert_call(value: dict, owner: dict | None = None) -> dict:
    """将旧 function 参数转换为对象，不能猜测供应商附加材料归属。

    Args:
        value (dict): 待转换的输入值。
        owner (dict | None): 旧专用材料的协议、地址和模型归属；缺失时禁止推断。

    Returns:
        dict: 参数为对象、恢复材料标明归属的业务工具调用。
    """
    function = value.get("function")
    if not isinstance(function, dict):
        raise MigrationError("旧工具调用缺少 function")
    args = function.get("arguments", "{}")
    args = json.loads(args) if isinstance(args, str) else args
    extra = value.get("extra_content")
    provider = None
    if extra:
        if owner is None:
            raise MigrationError("旧 extra_content 缺少归属，需要显式提供 source 协议、地址和模型")
        provider = ProviderData(**owner, payload={"extra_content": extra})
    return ToolCall(value.get("id"), function.get("name"), args, provider).to_dict()


def convert_content(value: Any, *, kind: str, refs: list[str] | None = None) -> list[dict]:
    """转换旧文字及图片，保留已知顺序与文件引用。

    Args:
        value (Any): 待转换的输入值。
        kind (str): 图片的业务类别。
        refs (list[str] | None): 与旧图片顺序一一对应的本地文件引用。

    Returns:
        list[dict]: 保留顺序与文件引用的新协议内容块。
    """
    blocks = [{"type": "text", "text": value}] if isinstance(value, str) else value
    if not isinstance(blocks, list):
        raise MigrationError("旧 content 既不是文字也不是数组")
    result = []
    images = 0
    for block in blocks:
        if not isinstance(block, dict):
            raise MigrationError("旧内容块必须是对象")
        if block.get("type") == "text":
            result.append(TextContent(block.get("text", "")).to_dict())
        elif block.get("type") == "image_url":
            image = block.get("image_url") or {}
            ref = refs[images] if refs and images < len(refs) else None
            result.append(ImageContent(image.get("url"), image.get("detail"), kind, ref).to_dict())
            images += 1
        else:
            raise MigrationError("不支持的旧内容块")
    if refs and len(refs) != images:
        raise MigrationError("旧图片数量与文件引用不一致")
    return result


def convert_message(raw: dict, owner: dict | None = None) -> dict:
    """转换独立旧消息；工具图片由调用方合并，不能直接保存为新消息。

    Args:
        raw (dict): 包含旧业务元数据的 v2 消息。
        owner (dict | None): 旧专用材料的协议、地址和模型归属；缺失时禁止推断。

    Returns:
        dict: 已通过新协议解码校验的完整业务消息。
    """
    kind = raw.get("_message_type")
    # 1. 先保留消息身份与时间，随后只解码该业务类型实际拥有的字段。
    result = {"message_type": kind, "message_id": raw.get("_message_id"), "created_at": raw.get("_created_at")}
    content = raw.get("content", "")
    if kind == "human":
        meta = raw.get("_human") or {}
        result.update(content=convert_content(content, kind="human", refs=meta.get("image_filenames")))
    elif kind == "assistant":
        result.update(content=convert_content(content, kind="human"),
            tool_calls=[convert_call(call, owner) for call in raw.get("tool_calls") or []], provider_data=None)
    elif kind == "tool":
        result.update(content=convert_content(content, kind="tool"), tool_name=raw.get("name"),
            tool_call_id=raw.get("tool_call_id"), compressible=bool((raw.get("_tool") or {}).get("compressible", False)))
    elif kind == "system":
        result["text"] = content
    elif kind == "context_summary":
        meta = raw.get("_context_summary") or {}
        result.update(content=content, summary_schema_version=meta.get("schema_version"),
            covered_through_message_id=meta.get("covered_through_message_id"),
            compression_ratio=meta.get("compression_ratio"))
    elif kind == "task_result":
        # 1.1. 旧后台通知把标签与正文拼在一起；必须匹配已保存的元数据再剥离标签。
        result.update(raw.get("_task") or {})
        labels = {"completed": "完成", "failed": "失败", "cancelled": "取消", "orphaned": "状态未知", "mixed": "状态汇总"}
        prefix = f"[后台任务{labels[result['status']]}] {result['task_name']}\n"
        if not isinstance(content, str) or not content.startswith(prefix):
            raise MigrationError("后台任务正文不符合旧格式")
        result["result"] = content[len(prefix):]
    elif kind in {"environment_perception", "subagent"}:
        # 1.2. 用旧业务字段重新生成前缀，不能靠换行或任意截取猜测实际正文。
        meta = raw.get("_environment" if kind == "environment_perception" else "_subagent") or {}
        result.update(meta)
        result["description" if kind == "environment_perception" else "result"] = ""
        prefix = notification_text(message_from_storage_format(result))
        if not isinstance(content, str) or not content.startswith(prefix):
            raise MigrationError("内部通知正文不符合旧格式")
        result["description" if kind == "environment_perception" else "result"] = content[len(prefix):]
    else:
        raise MigrationError(f"未知旧消息类型: {kind!r}")
    # 2. 全部类型交回新业务解码器检查，再返回可保存的独立 JSON。
    return message_from_storage_format(result).to_storage_format()


def media_parts(raw: dict) -> tuple[str, list[dict]]:
    """读取普通工具图片和明确结果关联，旧截图交给图片存储迁移处理。

    Args:
        raw (dict): 包含旧业务元数据的 v2 消息。

    Returns:
        tuple[str, list[dict]]: 明确目标消息编号及待合并图片内容块。
    """
    meta = raw.get("_tool_media") or {}
    associated = meta.get("associated_message_id")
    if not isinstance(associated, str) or not associated:
        raise MigrationError("工具图片缺少明确关联")
    payload = meta.get("payload") or {}
    refs = payload.get("storage_refs") or []
    kind = meta.get("media_kind")
    if kind == "screenshot" or payload.get("kind") == "compacted":
        raise MigrationError("旧截图契约已退出当前运行时，请通过图片存储迁移处理")
    if payload.get("kind") != "available":
        raise MigrationError("未知图片状态")
    converted = convert_content(raw.get("content"), kind=kind, refs=refs)
    images = [part for part in converted if part["type"] == "image_url"]
    if not images:
        raise MigrationError("可用工具图片没有图片内容")
    return associated, images


def merge_media(messages: list[dict], media: list[dict]) -> dict[str, str]:
    """按稳定身份合并图片，禁止邻接或工具名猜测关联。

    Args:
        messages (list[dict]): 新协议消息列表；按明确关联原地追加旧图片块。
        media (list[dict]): 等待按明确关联并入结果的旧图片消息。

    Returns:
        dict[str, str]: 旧图片编号到工具结果编号的映射；结果正文在 messages 内更新。
    """
    targets = {message["message_id"]: message for message in messages}
    if len(targets) != len(messages):
        raise MigrationError("消息编号重复，图片关联有歧义")
    remap = {}
    for raw in media:
        associated, parts = media_parts(raw)
        target = targets.get(associated)
        if target is None or target["message_type"] != "tool" or target["tool_name"] != raw["_tool_media"]["tool_name"]:
            raise MigrationError("工具图片关联不存在或工具名称不匹配")
        target["content"].extend(parts)
        if raw.get("_message_id"):
            remap[raw["_message_id"]] = associated
    return remap


def convert_checkpoint(value: dict, owner: dict | None = None, *, history_remap: dict[str, str] | None = None) -> dict:
    """转换完整 checkpoint，待处理工具与图片在同一状态恢复。

    Args:
        value (dict): 待转换的输入值。
        owner (dict | None): 旧专用材料的协议、地址和模型归属；缺失时禁止推断。
        history_remap (dict[str, str] | None): 本会话历史中已校验的图片编号到工具结果编号映射。

    Returns:
        dict: 通过运行时恢复校验的 v3 状态。
    """
    if value.get("schema_version") == 3:
        validate_legacy_checkpoint(value)
        return value
    if value.get("schema_version") != 2:
        raise MigrationError("仅支持 v2 到 v3 迁移")
    result = deepcopy(value)
    source = result.get("messages")
    if not isinstance(source, list):
        raise MigrationError("checkpoint.messages 必须是数组")
    messages = [convert_message(raw, owner) for raw in source if raw.get("_message_type") != "tool_media"]
    media = [raw for raw in source if raw.get("_message_type") == "tool_media"]
    # 1. 延迟图片来自已完成结果，合并后不再需要 API 注入队列。
    completed = {message["message_id"]: message for message in messages if message["message_type"] == "tool"}
    deferred_calls = set()
    for deferred in result.pop("_deferred_tool_media", []):
        target = completed.get(deferred.get("associated_message_id"))
        call_id = deferred.get("tool_call_id")
        if target is None or call_id != target["tool_call_id"] or call_id in deferred_calls:
            raise MigrationError("延迟图片调用关联无效或重复")
        deferred_calls.add(call_id)
        kind = {"system_screenshot": "screenshot", "mcp_tool_image": "mcp", "system_tool_image": "tool"}.get(deferred.get("image_message_name"))
        if kind is None:
            raise MigrationError("未知延迟图片类别")
        media.append({"_tool_media": {"tool_name": deferred["tool_name"], "media_kind": kind,
            "associated_message_id": deferred["associated_message_id"], "payload": {"kind": "available", "storage_refs": []}},
            "content": [{"type": "image_url", "image_url": {"url": url}} for url in deferred["image_urls"]]})
    remap = merge_media(messages, media)
    # 2. 摘要可能已移除覆盖末尾的图片，仅借用历史身份映射，不能把历史原图补回状态。
    for image_id, result_id in (history_remap or {}).items():
        if image_id in remap and remap[image_id] != result_id:
            raise MigrationError("checkpoint 和历史的图片关联不一致")
        remap[image_id] = result_id
    for message in messages:
        if message["message_type"] == "context_summary":
            message["covered_through_message_id"] = remap.get(message["covered_through_message_id"], message["covered_through_message_id"])
    result.update(schema_version=3, messages=messages, token_baseline=None)
    for field in ("pending_tool_calls", "pending_actions"):
        result[field] = [convert_call(call, owner) for call in result.get(field) or []]
    # 3. 按当前结构检查配对和审批状态，离线阶段的输出版本保持 v3。
    validate_legacy_checkpoint(result)
    return result


def history_to_legacy(row: dict) -> dict:
    """拼接旧历史的拆分字段，仅用于离线转换。

    Args:
        row (dict): 旧历史表的一条完整记录。

    Returns:
        dict: 仅供迁移解码使用的 v2 完整消息。
    """
    raw = json.loads(row["message_json"])
    domain = json.loads(row["domain_json"]) if row.get("domain_json") else {}
    raw.update(_message_type=row["message_type"], _message_id=row["message_id"], _created_at=row["created_at"])
    for name in ("human", "assistant", "tool", "task", "environment", "subagent"):
        if name in domain:
            raw["_" + name] = domain[name]
    if row["message_type"] == "tool_media":
        refs = json.loads(row["attachments_json"]) if row.get("attachments_json") else []
        raw["_tool_media"] = {"tool_name": domain.get("tool_name"), "media_kind": domain.get("media_kind"),
            "associated_message_id": row.get("associated_message_id"), "payload": {"kind": domain.get("payload_kind"), "storage_refs": refs}}
    return raw


def converted_history(db: sqlite3.Connection, owner: dict | None = None) -> tuple[list[dict], int]:
    """转换历史并合并图片，保留所有存活 SQL 行身份。

    Args:
        db (sqlite3.Connection): 调用方已经开启事务的 SQLite 连接。
        owner (dict | None): 旧专用材料的协议、地址和模型归属；缺失时禁止推断。

    Returns:
        tuple[list[dict], int]: 所有存活历史记录及合并删除的图片行数。
    """
    rows = [dict(row) for row in db.execute("SELECT * FROM conversation_messages ORDER BY id")]
    # 1. 会话是关联范围；相同消息编号出现在其他会话时不能借用其工具结果。
    media = {}
    for row in rows:
        if row["message_type"] == "tool_media":
            media.setdefault(row["session_id"], []).append(history_to_legacy(row))
    converted = []
    by_session = {}
    # 2. 图片行稍后并入完整结果，其余行保留 SQL 编号、时间和记录来源。
    for row in rows:
        if row["message_type"] == "tool_media":
            continue
        if row["record_kind"] != "legacy_tool_notice":
            row["payload"] = convert_message(history_to_legacy(row), owner)
            by_session.setdefault(row["session_id"], []).append(row["payload"])
        else:
            row["payload"] = json.loads(row["message_json"])
            # 2.1. 旧工具提示只有展示文字，不能伪造完整工具结果或交给 Agent 解码。
        converted.append(row)
    for session, images in media.items():
        # 3. 只按明确编号合并；缺失、重复或工具名称不一致都使整个迁移失败。
        merge_media(by_session.get(session, []), images)
    return converted, len(rows) - len(converted)


def migrate(db_path: Path, *, apply: bool = False, owner: dict | None = None) -> dict:
    """检查或原子迁移指定数据库，任何转换失败均回滚。

    Args:
        db_path (Path): 显式选择的数据库。
        apply (bool): 是否写入；False 使用 SQLite 只读 URI。
        owner (dict | None): 存在旧专用材料时显式提供的归属。
    Returns:
        dict: 计数和完整性报告，不包含正文或凭据。
    """
    if not db_path.is_file():
        raise MigrationError("数据库文件不存在")
    uri = db_path.resolve().as_uri() + ("?mode=rw" if apply else "?mode=ro")
    with sqlite3.connect(uri, uri=True) as db:
        db.row_factory = sqlite3.Row
        if not apply:
            db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN IMMEDIATE" if apply else "BEGIN")
        try:
            columns = {row[1] for row in db.execute("PRAGMA table_info(conversation_messages)")}
            old = "role" in columns
            # 1. 历史先验证明确关联，建立每个会话的编号映射；checkpoint 不读取历史图片正文。
            rows, merged = converted_history(db, owner) if old else ([], 0)
            history_maps: dict[str, dict[str, str]] = {}
            if old:
                for row in db.execute("SELECT session_id,message_id,associated_message_id FROM conversation_messages WHERE message_type='tool_media'"):
                    history_maps.setdefault(row[0], {})[row[1]] = row[2]
            ids = [row[0] for row in db.execute("SELECT id FROM checkpoints ORDER BY id")]
            pending = 0
            # 1. 先完整验证；检查模式不写入，应用模式持有写事务避免格式混合。
            for checkpoint_id in ids:
                value = json.loads(db.execute("SELECT state_json FROM checkpoints WHERE id=?", (checkpoint_id,)).fetchone()[0])
                pending += value.get("schema_version") != 3
                convert_checkpoint(value, owner, history_remap=history_maps.get(value.get("session_id")))
            if not old:
                for row in db.execute("SELECT message_json FROM conversation_messages WHERE record_kind<>'legacy_tool_notice'"):
                    message_from_storage_format(json.loads(row[0]))
            report = {"checkpoints": len(ids), "pending_checkpoints": pending, "merged_media_rows": merged,
                      "history_rows": db.execute("SELECT COUNT(*) FROM conversation_messages").fetchone()[0], "applied": apply}
            if apply and (old or pending):
                # 2. 重建业务历史列，存活编号与自增高水位保留；图片文件完全不参与。
                if old:
                    sequence = db.execute("SELECT seq FROM sqlite_sequence WHERE name='conversation_messages'").fetchone()
                    high = sequence[0] if sequence else 0
                    allowed = ",".join(f"'{value}'" for value in PROTOCOL_MESSAGE_TYPES)
                    db.execute(f"""CREATE TABLE conversation_messages_v3 (
                        id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, message_id TEXT NOT NULL,
                        created_at TEXT NOT NULL, record_kind TEXT NOT NULL CHECK(record_kind IN ('protocol','legacy_tool_notice')),
                        fidelity TEXT NOT NULL CHECK(fidelity IN ('exact','projected')), legacy_source_id TEXT,
                        message_type TEXT CHECK(message_type IN ({allowed})), message_json TEXT NOT NULL,
                        UNIQUE(session_id,message_id))""")
                    db.executemany("INSERT INTO conversation_messages_v3 VALUES (?,?,?,?,?,?,?,?,?)", [
                        (row["id"], row["session_id"], row["message_id"], row["created_at"], row["record_kind"], row["fidelity"], row["legacy_source_id"], row["message_type"], json.dumps(row["payload"], ensure_ascii=False))
                        for row in rows])
                    db.execute("DROP TABLE conversation_messages")
                    db.execute("ALTER TABLE conversation_messages_v3 RENAME TO conversation_messages")
                    db.execute("UPDATE sqlite_sequence SET seq=MAX(seq,?) WHERE name='conversation_messages'", (high,))
                    db.execute("CREATE INDEX idx_conversation_messages_session_id ON conversation_messages(session_id,id)")
                    db.execute("CREATE INDEX idx_conversation_messages_session_time ON conversation_messages(session_id,created_at,id)")
                for checkpoint_id in ids:
                    value = json.loads(db.execute("SELECT state_json FROM checkpoints WHERE id=?", (checkpoint_id,)).fetchone()[0])
                    db.execute("UPDATE checkpoints SET state_json=? WHERE id=?", (json.dumps(convert_checkpoint(value, owner, history_remap=history_maps.get(value.get("session_id"))), ensure_ascii=False), checkpoint_id))
                db.execute("PRAGMA user_version=3")
            # 3. 最终完整性失败同样回滚；重复执行不会再次合并或改写已迁移状态。
            integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise MigrationError("SQLite 完整性检查失败")
            report["integrity"] = integrity
            db.commit() if apply else db.rollback()
            return report
        except BaseException:
            db.rollback()
            raise


def main() -> int:
    """解析显式目标和停机声明，输出不含消息内容的迁移报告。

    Returns:
        int: 迁移成功返回 0，未提交数据的失败返回 1。
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--check", action="store_true")
    actions.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-app-stopped", action="store_true")
    parser.add_argument("--source-api-format")
    parser.add_argument("--source-endpoint")
    parser.add_argument("--source-model")
    args = parser.parse_args()
    if args.apply and not args.confirm_app_stopped:
        parser.error("--apply 必须确认桌面与后端已停止")
    values = (args.source_api_format, args.source_endpoint, args.source_model)
    if any(values) and not all(values):
        parser.error("source 归属必须同时提供协议、地址和模型")
    owner = dict(zip(("api_format", "endpoint", "model"), values)) if all(values) else None
    try:
        print(json.dumps(migrate(args.db, apply=args.apply, owner=owner), ensure_ascii=False))
        return 0
    except (ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        print(f"迁移终止，未提交数据：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
