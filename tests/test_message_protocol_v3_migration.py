"""验证离线迁移的保真、只读检查与失败回滚。"""

import json
import sqlite3
from pathlib import Path

import pytest

from scripts.migrate_message_protocol_v3 import migrate, MigrationError, convert_checkpoint
from app.agent.state import AgentState


def legacy_fixture(path: Path, *, invalid: bool = False, compacted: bool = False) -> dict:
    """创建独立 v2 数据库，不读取生产数据。"""
    timestamp = "2026-10-12T02:00:00+00:00"
    base = {"_created_at": timestamp}
    assistant = {**base, "_message_id": "assistant", "_message_type": "assistant", "role": "assistant", "content": "", "tool_calls": [{"id": "call", "type": "function", "function": {"name": "screenshot", "arguments": "{}"}}], "_assistant": {"origin_channel": "desktop"}}
    tool = {**base, "_message_id": "tool", "_message_type": "tool", "role": "tool", "name": "screenshot", "tool_call_id": "call", "content": "截图成功", "_tool": {"compressible": True}}
    image = {**base, "_message_id": "image", "_message_type": "tool_media", "role": "user", "content": [{"type": "text", "text": "图片"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}}], "_tool_media": {"tool_name": "screenshot", "media_kind": "screenshot", "associated_message_id": "missing" if invalid else "tool", "payload": {"kind": "available", "storage_refs": ["screenshot/a.png"]}}}
    checkpoint_image = json.loads(json.dumps(image))
    if compacted:
        checkpoint_image["_tool_media"]["payload"]["kind"] = "compacted"
        checkpoint_image["content"] = "[系统消息]已被压缩的旧截图"
    state = {"schema_version": 2, "session_id": "s", "messages": [assistant, tool, checkpoint_image], "pending_tool_calls": [], "pending_actions": [], "_deferred_tool_media": [], "created_at": timestamp, "updated_at": timestamp}
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE checkpoints(id INTEGER PRIMARY KEY,session_id TEXT,state_json TEXT,checkpoint_type TEXT,created_at TEXT)")
        db.execute("INSERT INTO checkpoints VALUES(1,'s',?,'completed',?)", (json.dumps(state), timestamp))
        db.execute("CREATE TABLE conversation_messages(id INTEGER PRIMARY KEY AUTOINCREMENT,session_id TEXT,message_id TEXT,created_at TEXT,role TEXT,message_json TEXT,record_kind TEXT,fidelity TEXT,associated_message_id TEXT,attachments_json TEXT,legacy_source_id TEXT,message_type TEXT,domain_json TEXT)")
        for index, raw in enumerate((assistant, tool, image), 1):
            wire = {key: value for key, value in raw.items() if not key.startswith('_')}
            meta = raw.get('_tool_media')
            domain = {"tool_name": "screenshot", "media_kind": "screenshot", "payload_kind": "available"} if meta else {key[1:]: value for key, value in raw.items() if key in {'_tool', '_assistant'}}
            db.execute("INSERT INTO conversation_messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (index,'s',raw['_message_id'],timestamp,raw['role'],json.dumps(wire),'tool_image' if meta else 'protocol','exact',meta['associated_message_id'] if meta else None,json.dumps(['screenshot/a.png']) if meta else None,None,raw['_message_type'],json.dumps(domain)))
        db.execute("UPDATE sqlite_sequence SET seq=100 WHERE name='conversation_messages'")
    return state


def test_check_is_read_only_and_apply_merges_with_ids_preserved(tmp_path: Path) -> None:
    """检查不改文件；迁移合并图片并保持高水位，重复应用无额外改变。"""
    path = tmp_path / "old.sqlite"
    legacy_fixture(path)
    before = path.read_bytes()
    assert migrate(path)["merged_media_rows"] == 1
    assert path.read_bytes() == before
    migrate(path, apply=True)
    with sqlite3.connect(path) as db:
        rows = db.execute("SELECT id,message_id,message_json FROM conversation_messages ORDER BY id").fetchall()
        assert [(row[0], row[1]) for row in rows] == [(1, "assistant"), (2, "tool")]
        assert json.loads(rows[1][2])["content"][1]["storage_ref"] == "screenshot/a.png"
        assert db.execute("SELECT seq FROM sqlite_sequence WHERE name='conversation_messages'").fetchone()[0] == 100
    assert migrate(path, apply=True)["pending_checkpoints"] == 0


def test_missing_association_aborts_without_schema_or_data_changes(tmp_path: Path) -> None:
    """非法关联使迁移整体失败，原表与原 checkpoint 不变。"""
    path = tmp_path / "bad.sqlite"
    legacy_fixture(path, invalid=True)
    before = path.read_bytes()
    with pytest.raises(MigrationError, match="关联"):
        migrate(path, apply=True)
    assert path.read_bytes() == before


def test_compacted_checkpoint_does_not_rehydrate_original_history(tmp_path: Path) -> None:
    """历史仍有原图，checkpoint 压缩图片不能通过迁移重载。"""
    path = tmp_path / "compact.sqlite"
    state = legacy_fixture(path, compacted=True)
    restored = AgentState.from_checkpoint(convert_checkpoint(state))
    image = restored.messages[-1].content[-1]
    assert image.context_state == "compacted" and image.url == ""
    migrate(path, apply=True)


def test_checkpoint_update_failure_rolls_back_rebuilt_history(tmp_path: Path) -> None:
    """重建历史后写 checkpoint 失败，DDL、行数和原始状态一起回滚。"""
    path = tmp_path / "abort.sqlite"
    legacy_fixture(path)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TRIGGER reject_migration BEFORE UPDATE ON checkpoints BEGIN SELECT RAISE(ABORT,'reject'); END")
    before = path.read_bytes()
    with pytest.raises(sqlite3.IntegrityError, match="reject"):
        migrate(path, apply=True)
    assert path.read_bytes() == before


def test_deferred_image_is_merged_before_pending_batch_recovers(tmp_path: Path) -> None:
    """旧延迟队列图片并入已完成结果，第二项请求仍处于待执行状态。"""
    state = legacy_fixture(tmp_path / "deferred.sqlite")
    media = state["messages"].pop()
    state["messages"][0]["tool_calls"].append({"id": "next", "function": {"name": "read_file", "arguments": "{}"}})
    state["pending_tool_calls"] = [state["messages"][0]["tool_calls"][-1]]
    state["_deferred_tool_media"] = [{"tool_call_id": "call", "tool_name": "screenshot", "associated_message_id": "tool",
        "image_message_name": "system_screenshot", "image_urls": [media["content"][1]["image_url"]["url"]]}]
    restored = AgentState.from_checkpoint(convert_checkpoint(state))
    assert restored.pending_tool_calls[0].id == "next"
    assert restored.messages[-1].content[-1].media_kind == "screenshot"


def test_summary_of_removed_image_maps_identity_without_restoring_messages(tmp_path: Path) -> None:
    """摘要覆盖的图片已经移除时，只从历史映射编号，不恢复工具批次。

    Args:
        tmp_path (Path): Pytest 提供的独立数据库目录。
    """
    path = tmp_path / "summary.sqlite"
    state = legacy_fixture(path)
    state["messages"] = [{"_message_id": "summary", "_message_type": "context_summary",
        "_created_at": state["created_at"], "role": "user", "content": "已有摘要",
        "_context_summary": {"schema_version": 1, "covered_through_message_id": "image",
            "covered_effective_date": "2026-10-12", "compression_ratio": 0.5}}]
    with sqlite3.connect(path) as db:
        db.execute("UPDATE checkpoints SET state_json=?", (json.dumps(state),))
    migrate(path, apply=True)
    with sqlite3.connect(path) as db:
        restored = AgentState.from_checkpoint(json.loads(db.execute("SELECT state_json FROM checkpoints").fetchone()[0]))
    assert len(restored.messages) == 1
    assert restored.messages[0].covered_through_message_id == "tool"


def test_deferred_image_with_wrong_call_aborts_migration(tmp_path: Path) -> None:
    """延迟图片即使关联编号存在，调用编号错误也必须拒绝。

    Args:
        tmp_path (Path): 临时数据库目录。
    """
    state = legacy_fixture(tmp_path / "wrong_call.sqlite")
    media = state["messages"].pop()
    state["_deferred_tool_media"] = [{"tool_call_id": "wrong", "tool_name": "screenshot",
        "associated_message_id": "tool", "image_message_name": "system_screenshot",
        "image_urls": [media["content"][1]["image_url"]["url"]]}]
    with pytest.raises(MigrationError, match="关联"):
        convert_checkpoint(state)
