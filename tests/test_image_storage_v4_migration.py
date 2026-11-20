from scripts.migrate_memory_state_v5 import convert_checkpoint
"""通过离线迁移入口验证旧图片转换、文件操作及数据库事务。"""

import json
from pathlib import Path
import sqlite3

import pytest

from app.agent.state import AgentState
from app.agent.state.messages import ImageContent, ToolCall
from app.crud.agent_storage_sql import agent_state_schema
from app.schemas.agent_storage import AgentStorageScope
from tests.model_capture import result_parts


def make_v3_fixture(path: Path, images: Path) -> dict:
    """建立包含压缩截图、协议历史及旧存档表的隔离 v3 数据库。

    Args:
        path (Path): 测试数据库。
        images (Path): 测试图片根目录。
    Returns:
        dict: 迁移前的主会话 checkpoint。
    """
    source = images / "screenshot"
    source.mkdir(parents=True)
    (source / "screen.png").write_bytes(b"screen")
    (source / "unreferenced.png").write_bytes(b"extra")
    state = AgentState.create_new("migration")
    state.add_user_message("观察")
    call = ToolCall("call", "screenshot", {})
    state.record_assistant_step("", (call,))
    state.record_tool_result(call, result_parts("观察结果", (), "tool"))
    raw = state.to_checkpoint()
    raw["schema_version"] = 3
    raw["long_term_memory_turn_counter"] = 0
    picture = {"type": "image_url", "image_url": {"url": ""}, "media_kind": "screenshot",
               "storage_ref": "screenshot/screen.png", "context_state": "compacted"}
    raw["messages"][-1]["content"].append(picture)
    with sqlite3.connect(path) as db:
        for scope in AgentStorageScope:
            for statement in agent_state_schema(scope):
                db.execute(statement)
        db.execute("CREATE TABLE chat_history(id INTEGER PRIMARY KEY,content TEXT)")
        db.execute("INSERT INTO chat_history VALUES(1,'旧存档')")
        db.execute("CREATE TABLE unrelated(id INTEGER PRIMARY KEY,value TEXT)")
        db.execute("INSERT INTO unrelated VALUES(7,'保留')")
        db.execute("INSERT INTO checkpoints(session_id,state_json,checkpoint_type) VALUES(?,?,'completed')",
                   ("migration", json.dumps(raw)))
        for message in raw["messages"]:
            db.execute("INSERT INTO conversation_messages(session_id,message_id,created_at,record_kind,fidelity,message_type,message_json) "
                       "VALUES(?,?,?,'protocol','exact',?,?)", ("migration", message["message_id"],
                       message["created_at"], message["message_type"], json.dumps(message)))
        db.execute("PRAGMA user_version=3")
    return raw


def test_migration_checks_then_restores_compacted_images_and_moves_all_files(tmp_path: Path) -> None:
    """只读检查无副作用，迁移恢复压缩图片、删除旧表且可重复执行。

    Args:
        tmp_path (Path): 隔离数据库和图片目录。
    """
    from scripts.migrate_image_storage_v4 import migrate

    path, images = tmp_path / "state.sqlite3", tmp_path / "images"
    original = make_v3_fixture(path, images)
    before = path.read_bytes()
    report = migrate(path, images)
    assert not report["applied"]
    assert report["changed_rows"] == 2
    assert path.read_bytes() == before
    assert not (images / "tool").exists()
    report = migrate(path, images, apply=True)
    assert report["integrity"] == "ok"
    assert report["files_copied"] == 2
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 4
        raw = json.loads(db.execute("SELECT state_json FROM checkpoints").fetchone()[0])
        assert db.execute("SELECT value FROM unrelated").fetchone()[0] == "保留"
        assert db.execute("SELECT name FROM sqlite_master WHERE name='chat_history'").fetchone() is None
    restored = AgentState.from_checkpoint(convert_checkpoint(raw))
    picture = restored.messages[-1].content[-1]
    assert isinstance(picture, ImageContent)
    assert picture.storage_ref == "tool/screen.png" and picture.url is None
    assert [m.message_id for m in restored.messages] == [m["message_id"] for m in original["messages"]]
    assert (images / "tool/screen.png").read_bytes() == b"screen"
    assert (images / "tool/unreferenced.png").read_bytes() == b"extra"
    assert not (images / "screenshot").exists()
    after = path.read_bytes()
    assert migrate(path, images)["changed_rows"] == 0
    assert migrate(path, images, apply=True)["changed_rows"] == 0
    assert path.read_bytes() == after


def test_migration_covers_child_inline_images_and_temporary_background(tmp_path: Path) -> None:
    """父子消息和临时背景共享内联文件，压缩缺图保留文字及其他内容。

    Args:
        tmp_path (Path): 隔离数据库和图片目录。
    """
    from scripts.migrate_image_storage_v4 import migrate

    path, images = tmp_path / "state.sqlite3", tmp_path / "images"
    raw = make_v3_fixture(path, images)
    inline = {"type": "image_url", "image_url": {"url": "data:image/png;base64,eHl6", "detail": "low"},
              "media_kind": "tool"}
    missing = {"type": "image_url", "image_url": {"url": ""}, "media_kind": "screenshot",
               "context_state": "compacted", "storage_ref": "screenshot/missing.png"}
    raw["messages"][-1]["content"].extend([inline, missing, {"type": "text", "text": "末尾文字"}])
    raw["temporary_context"] = {"plugin.screen": {"title": "背景", "text": "状态", "images": [
        {"url": "data:image/jpeg;base64,eHl6", "media_kind": "environment", "detail": "high"}]}}
    with sqlite3.connect(path) as db:
        db.execute("UPDATE checkpoints SET state_json=?", (json.dumps(raw),))
        db.execute("INSERT INTO subagent_checkpoints(session_id,state_json,checkpoint_type) VALUES(?,?,'completed')",
                   ("migration", json.dumps(raw)))
        db.execute("INSERT INTO subagent_conversation_messages SELECT * FROM conversation_messages")
        db.execute("UPDATE subagent_conversation_messages SET message_json=? WHERE message_type='tool'",
                   (json.dumps(raw["messages"][-1]),))
    report = migrate(path, images, apply=True)
    assert "tool/missing.png" in report["missing_refs"]
    with sqlite3.connect(path) as db:
        parent = json.loads(db.execute("SELECT state_json FROM checkpoints").fetchone()[0])
        child = json.loads(db.execute("SELECT state_json FROM subagent_checkpoints").fetchone()[0])
        history = json.loads(db.execute("SELECT message_json FROM subagent_conversation_messages WHERE message_type='tool'").fetchone()[0])
    assert "base64" not in json.dumps([parent, child, history])
    picture = parent["messages"][-1]["content"][-3]
    assert picture["storage_ref"] == child["messages"][-1]["content"][-3]["storage_ref"] == history["content"][-3]["storage_ref"]
    assert (images / picture["storage_ref"]).read_bytes() == b"xyz"
    assert parent["messages"][-1]["content"][-2] == {"type": "text", "text": "[图片不可用：tool/missing.png]"}
    assert parent["messages"][-1]["content"][-1]["text"] == "末尾文字"
    background = AgentState.from_checkpoint(convert_checkpoint(child)).temporary_context["plugin.screen"].images[0]
    assert background.url is None and background.storage_ref.startswith("environment/")


def test_migration_validates_database_before_committing_version_and_can_resume(tmp_path: Path) -> None:
    """数据库写入后发现非法状态即回滚，已复制文件可在修复原因后复用。

    Args:
        tmp_path (Path): 隔离数据库和图片目录。
    """
    from scripts.migrate_image_storage_v4 import migrate

    path, images = tmp_path / "state.sqlite3", tmp_path / "images"
    make_v3_fixture(path, images)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TRIGGER corrupt_state AFTER UPDATE ON checkpoints "
                   "BEGIN UPDATE checkpoints SET state_json='{}' WHERE id=NEW.id; END")
    with pytest.raises(ValueError):
        migrate(path, images, apply=True)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3
        assert json.loads(db.execute("SELECT state_json FROM checkpoints").fetchone()[0])["schema_version"] == 3
        assert db.execute("SELECT content FROM chat_history").fetchone()[0] == "旧存档"
        db.execute("DROP TRIGGER corrupt_state")
    assert (images / "screenshot/screen.png").is_file()
    assert (images / "tool/screen.png").is_file()
    assert migrate(path, images, apply=True)["files_copied"] == 0


@pytest.mark.parametrize("apply", [False, True])
def test_migration_refuses_conflicting_file_without_changing_database(tmp_path: Path, apply: bool) -> None:
    """同名目标保存其他内容时，检查和执行都在修改前拒绝。

    Args:
        tmp_path (Path): 隔离数据库和图片目录。
        apply (bool): 本次检查只读或正式执行入口。
    """
    from scripts.migrate_image_storage_v4 import migrate, MigrationError

    path, images = tmp_path / "state.sqlite3", tmp_path / "images"
    make_v3_fixture(path, images)
    (images / "tool").mkdir()
    (images / "tool/screen.png").write_bytes(b"unrelated")
    before = path.read_bytes()
    with pytest.raises(MigrationError, match="冲突"):
        migrate(path, images, apply=apply)
    assert path.read_bytes() == before
    assert (images / "tool/screen.png").read_bytes() == b"unrelated"
    assert (images / "screenshot/screen.png").read_bytes() == b"screen"


def test_cleanup_failure_can_resume_after_database_has_committed(tmp_path: Path, monkeypatch) -> None:
    """旧文件清理失败保留 v4，重复执行继续清理且不重写业务记录。

    Args:
        tmp_path (Path): 隔离数据库和图片目录。
        monkeypatch (pytest.MonkeyPatch): 让一次旧文件删除失败。
    """
    from scripts.migrate_image_storage_v4 import migrate

    path, images = tmp_path / "state.sqlite3", tmp_path / "images"
    make_v3_fixture(path, images)
    original_unlink = Path.unlink

    def unlink(file: Path, *args, **kwargs) -> None:
        """模拟旧截图被暂时占用，其他文件正常清理。

        Args:
            file (Path): 本次要删除的文件。
            args (tuple): 文件删除的位置参数。
            kwargs (dict): 文件删除的选项。
        Raises:
            PermissionError: 用例指定的旧截图被占用。
        """
        if file.name == "screen.png" and file.parent.name == "screenshot":
            raise PermissionError("in use")
        original_unlink(file, *args, **kwargs)

    with monkeypatch.context() as scoped:
        scoped.setattr(Path, "unlink", unlink)
        report = migrate(path, images, apply=True)
    assert report["remaining_source_files"] == ["screenshot/screen.png"]
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 4
    repeated = migrate(path, images, apply=True)
    assert repeated["changed_rows"] == 0
    assert repeated["files_copied"] == 0 and repeated["remaining_source_files"] == []


def test_migration_preserves_ordinary_missing_references_and_legacy_notices(tmp_path: Path) -> None:
    """普通缺图继续保存引用，旧工具提示原样保留，来源参数不被递归转换。

    Args:
        tmp_path (Path): 隔离数据库和图片目录。
    """
    from scripts.migrate_image_storage_v4 import migrate

    path, images = tmp_path / "state.sqlite3", tmp_path / "images"
    raw = make_v3_fixture(path, images)
    raw["messages"][-1]["content"].append({"type": "image_url", "image_url": {"url": "tool/absent.png"},
                                          "media_kind": "tool"})
    unchanged = {"type": "image_url", "context_state": "compacted", "media_kind": "screenshot"}
    raw["messages"][-2]["tool_calls"][0]["args"]["business_payload"] = unchanged
    with sqlite3.connect(path) as db:
        db.execute("UPDATE checkpoints SET state_json=?", (json.dumps(raw),))
        db.execute("INSERT INTO conversation_messages(session_id,message_id,created_at,record_kind,fidelity,message_type,message_json) "
                   "VALUES('migration','old','2026-01-01','legacy_tool_notice','projected',NULL,?)", (json.dumps({"content": "旧提示"}),))
    report = migrate(path, images, apply=True)
    assert report["missing_refs"] == ["tool/absent.png"]
    with sqlite3.connect(path) as db:
        checkpoint = json.loads(db.execute("SELECT state_json FROM checkpoints").fetchone()[0])
        assert db.execute("SELECT message_json FROM conversation_messages WHERE message_id='old'").fetchone()[0] == json.dumps({"content": "旧提示"})
    assert checkpoint["messages"][-1]["content"][-1]["storage_ref"] == "tool/absent.png"
    assert checkpoint["messages"][-2]["tool_calls"][0]["args"]["business_payload"] == unchanged


def test_interrupted_file_copy_can_be_retried_without_partial_target(tmp_path: Path, monkeypatch) -> None:
    """图片写入被中断时保留旧库和源图，重试能够完成而无同名残片冲突。

    Args:
        tmp_path (Path): 隔离数据库和图片目录。
        monkeypatch (pytest.MonkeyPatch): 在图片写到一半时模拟用户中断。
    """
    from contextlib import contextmanager
    from types import SimpleNamespace
    from scripts.migrate_image_storage_v4 import migrate

    path, images = tmp_path / "state.sqlite3", tmp_path / "images"
    make_v3_fixture(path, images)
    original_open = Path.open

    @contextmanager
    def open_file(file: Path, mode: str = "r", *args, **kwargs):
        """打开真实文件，只对本用例目标图片制造半途写入中断。

        Args:
            file (Path): 当前文件。
            mode (str): 文件打开模式。
            args (tuple): 其他文件打开参数。
            kwargs (dict): 编码等文件选项。
        Yields:
            IO: 正常文件或模拟中断的写入对象。
        """
        with original_open(file, mode, *args, **kwargs) as stream:
            if file.parent.name == "tool" and "x" in mode:
                def write(data: bytes) -> int:
                    """写出部分内容后触发执行中断。

                    Args:
                        data (bytes): 待保存图片字节。
                    Raises:
                        KeyboardInterrupt: 本用例模拟的进程中断。
                    """
                    stream.write(data[:2])
                    raise KeyboardInterrupt()
                yield SimpleNamespace(write=write)
            else:
                yield stream

    with monkeypatch.context() as scoped:
        scoped.setattr(Path, "open", open_file)
        with pytest.raises(KeyboardInterrupt):
            migrate(path, images, apply=True)
    assert not (images / "tool/screen.png").exists()
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3
    assert migrate(path, images, apply=True)["files_copied"] == 2


def test_compacted_images_without_references_become_text_in_messages_and_background(tmp_path: Path) -> None:
    """旧压缩图片没有保存过文件时，消息和背景继续保留缺图说明。

    Args:
        tmp_path (Path): 隔离数据库和图片目录。
    """
    from scripts.migrate_image_storage_v4 import migrate

    path, images = tmp_path / "state.sqlite3", tmp_path / "images"
    raw = make_v3_fixture(path, images)
    raw["messages"][-1]["content"][-1].pop("storage_ref")
    raw["temporary_context"] = {"plugin.screen": {"title": "背景", "text": "已有文字", "images": [
        {"url": "", "media_kind": "screenshot", "context_state": "compacted"}]}}
    with sqlite3.connect(path) as db:
        db.execute("UPDATE checkpoints SET state_json=?", (json.dumps(raw),))
    migrate(path, images, apply=True)
    with sqlite3.connect(path) as db:
        raw = json.loads(db.execute("SELECT state_json FROM checkpoints").fetchone()[0])
    assert raw["messages"][-1]["content"][-1] == {"type": "text", "text": "[图片不可用：旧图片缺少文件引用]"}
    restored = AgentState.from_checkpoint(convert_checkpoint(raw))
    assert restored.temporary_context["plugin.screen"].images == ()
    assert restored.temporary_context["plugin.screen"].text == "已有文字\n[图片不可用：旧图片缺少文件引用]"
