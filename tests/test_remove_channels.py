"""验证离线删除渠道结构时保留现有对话、输入和审批事实。"""

import json
import sqlite3
from copy import deepcopy
from pathlib import Path

import pytest

from app.agent.state.messages import ToolCall, message_from_storage_format
from app.agent.state.model import AgentState
from app.crud.session_inbox_dao import INBOX_SCHEMA_SQL
from scripts.remove_channels import migrate


def create_old_database(path: Path) -> dict:
    """创建包含主子数据、排队输入和有效审批的迁移前数据库。

    Args:
        path (Path): 当前用例专用数据库。

    Returns:
        dict: 清理渠道字段后应恢复的 checkpoint。
    """
    state = AgentState.create_new("s")
    state.add_user_message("正文 origin_channel 保持原样")
    call = ToolCall("call", "demo", {"origin_channel": "工具参数"})
    state.record_assistant_step("准备调用", (call,))
    state.clear_pending_tool_calls()
    state.set_interrupt({"type": "authorization_request", "request_id": "approval",
        "tool_call_id": call.id, "tool_name": call.name, "tool_args": call.args,
        "resume_state": {"origin_channel": "工具恢复材料"}})
    state.update_extra({"plugin": {"origin_channel": "插件状态"}})
    expected = state.to_checkpoint()
    old = deepcopy(expected)
    for message in old["messages"]:
        message["origin_channel"] = "desktop"
    old["interrupt_data"]["origin_channel"] = "desktop"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE channel_subscriptions(session_id TEXT, channel TEXT, enabled INTEGER)")
        db.execute("INSERT INTO channel_subscriptions VALUES('s','external',1)")
        for prefix in ("", "subagent_"):
            inbox = prefix + "session_inbox"
            db.execute(INBOX_SCHEMA_SQL.replace("session_inbox", inbox).replace(
                "payload_json TEXT NOT NULL", "origin_channel TEXT NOT NULL, payload_json TEXT NOT NULL"))
            db.execute(f"CREATE INDEX {prefix}input_order ON {inbox}(session_id,status,id)")
            db.execute(f"INSERT INTO {inbox}(id,session_id,kind,origin_channel,payload_json,delivery_mode,created_at) "
                "VALUES(99,'s','human','desktop','{\"message\":\"待处理正文\"}','queued','date')")
            db.execute(f"UPDATE sqlite_sequence SET seq=150 WHERE name=?", (inbox,))
            db.execute(f"CREATE TABLE {prefix}conversation_messages(id INTEGER PRIMARY KEY, message_json TEXT)")
            for index, message in enumerate(old["messages"]):
                db.execute(f"INSERT INTO {prefix}conversation_messages VALUES(?,?)", (index, json.dumps(message)))
            db.execute(f"CREATE TABLE {prefix}checkpoints(id INTEGER PRIMARY KEY, state_json TEXT)")
            db.execute(f"INSERT INTO {prefix}checkpoints VALUES(1,?)", (json.dumps(old),))
    return expected


def test_migration_preserves_input_ids_approval_material_and_nested_fields(tmp_path: Path) -> None:
    """清理主子渠道结构后仍能恢复完整审批，编号和非渠道业务内容保持原值。

    Args:
        tmp_path (Path): 测试专用目录。
    """
    path = tmp_path / "old.sqlite3"
    expected = create_old_database(path)
    before = path.read_bytes()
    report = migrate(path)
    assert report["messages_updated"] == 4 and report["checkpoints_updated"] == 2
    assert path.read_bytes() == before
    assert migrate(path, apply=True)["applied"] is True
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE name='channel_subscriptions'").fetchone() is None
        for prefix in ("", "subagent_"):
            for (raw,) in db.execute(f"SELECT message_json FROM {prefix}conversation_messages ORDER BY id"):
                assert message_from_storage_format(json.loads(raw)).to_storage_format() in expected["messages"]
            restored = json.loads(db.execute(f"SELECT state_json FROM {prefix}checkpoints").fetchone()[0])
            assert restored == expected
            assert AgentState.from_checkpoint(restored).get_pending_authorization("approval") is not None
            row = db.execute(f"SELECT id,delivery_mode,status,payload_json FROM {prefix}session_inbox").fetchone()
            assert row == (99, "queued", "pending", '{"message":"待处理正文"}')
            assert db.execute("SELECT seq FROM sqlite_sequence WHERE name=?", (prefix+"session_inbox",)).fetchone()[0] == 150
            assert db.execute("SELECT name FROM sqlite_master WHERE name=?", (prefix+"input_order",)).fetchone()
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    again = migrate(path, apply=True)
    assert again["messages_updated"] == again["checkpoints_updated"] == 0
    assert again["columns_removed"] == [] and again["tables_dropped"] == []


def test_migration_rolls_back_schema_and_rows_when_later_checkpoint_is_invalid(tmp_path: Path) -> None:
    """后面的 checkpoint 损坏时，前面已经执行的表和消息清理一起回滚。

    Args:
        tmp_path (Path): 测试专用目录。
    """
    path = tmp_path / "invalid.sqlite3"
    create_old_database(path)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE subagent_checkpoints SET state_json='invalid JSON'")
        db.commit()
        before = list(db.iterdump())
    with pytest.raises(ValueError):
        migrate(path, apply=True)
    with sqlite3.connect(path) as db:
        assert list(db.iterdump()) == before


def test_migration_handles_missing_subscription_table_and_partial_cleanup(tmp_path: Path) -> None:
    """已删订阅表和一个 Inbox 渠道列时，其余数据仍能完成清理。

    Args:
        tmp_path (Path): 测试专用目录。
    """
    path = tmp_path / "partial.sqlite3"
    create_old_database(path)
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE channel_subscriptions")
        db.execute("ALTER TABLE subagent_session_inbox DROP COLUMN origin_channel")
    report = migrate(path, apply=True)
    assert report["tables_dropped"] == []
    assert report["columns_removed"] == ["session_inbox"]
    assert report["messages_updated"] == 4 and report["checkpoints_updated"] == 2


def test_migration_explicit_missing_path_does_not_create_a_database(tmp_path: Path) -> None:
    """路径不存在时报告错误，避免将迁移误当作新库初始化。

    Args:
        tmp_path (Path): 测试专用目录。
    """
    path = tmp_path / "missing.sqlite3"
    with pytest.raises(sqlite3.OperationalError):
        migrate(path, apply=True)
    assert not path.exists()
