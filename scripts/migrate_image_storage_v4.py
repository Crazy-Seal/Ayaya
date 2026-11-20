"""停机后将共享主库的图片改为文件引用，默认只读检查，--apply 执行迁移。"""

from __future__ import annotations

import argparse
import base64
import binascii
from contextlib import closing
from copy import deepcopy
from dataclasses import dataclass, field
import json
import hashlib
from pathlib import Path
import sqlite3
import sys
from typing import TypedDict
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent.state.messages import ImageContent, TextContent, message_from_history
from scripts.legacy_checkpoint_validation import validate_legacy_checkpoint

CHECKPOINT_SCHEMA_VERSION = 4
from app.agent.utils.domain.images import resolve_image_path
from app.schemas.agent_storage import AgentStorageScope
from app.schemas.message_types import LEGACY_TOOL_NOTICE_KIND, MediaKind


class MigrationError(ValueError):
    """图片或旧记录无法安全转换，当前数据库事务必须回滚。"""


class MigrationReport(TypedDict):
    """提供可保存的迁移统计和文件映射，供副本演练与正式核验使用。"""

    source_version: int
    target_version: int
    table_rows: dict[str, int]
    changed_rows: int
    restored_images: int
    missing_refs: list[str]
    files_copied: int
    file_mapping: dict[str, str]
    dropped_legacy_history: bool
    remaining_source_files: list[str]
    applied: bool
    integrity: str


@dataclass
class _ImageFiles:
    """准备迁移涉及的文件，全部转换验证通过后再写出副本。

    Attributes:
        root (Path): 本次迁移的图片根目录。
        targets (dict[str, bytes]): 各目标文件的预期字节。
        sources (dict[str, str]): 待清理的旧截图与目标路径映射。
        missing (set[str]): 旧记录引用但未找到的图片。
        restored (int): 从旧压缩状态恢复的图片块数量。
    """

    root: Path
    targets: dict[str, bytes] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)
    missing: set[str] = field(default_factory=set)
    restored: int = 0

    def stage(self, reference: str, data: bytes) -> None:
        """确认目标文件可复用或可新建，内容冲突时终止本次迁移。

        Args:
            reference (str): 新版允许使用的图片引用。
            data (bytes): 转换后应当保存的完整图片字节。
        Raises:
            MigrationError: 同名目标已经保存其他内容。
        """
        target = resolve_image_path(reference, images_dir=self.root)
        expected = self.targets.get(reference)
        if ((expected is not None and expected != data)
                or (target.exists() and (not target.is_file() or target.read_bytes() != data))):
            raise MigrationError(f"目标图片内容冲突: {reference}")
        self.targets[reference] = data

    def collect_screenshots(self) -> None:
        """准备旧截图目录的全部文件，包括未被当前业务记录引用的文件。

        Raises:
            MigrationError: 目录越界、存在非普通文件或目标内容冲突。
        """
        source = self.root / "screenshot"
        if not source.exists():
            return
        if source.resolve().parent != self.root.resolve():
            raise MigrationError("旧截图目录超出图片根目录")
        for path in sorted(source.iterdir()):
            if not path.is_file() or path.is_symlink():
                raise MigrationError(f"旧截图目录存在无法迁移的文件: {path.name}")
            reference = f"tool/{path.name}"
            self.stage(reference, path.read_bytes())
            self.sources[f"screenshot/{path.name}"] = reference

    def convert_image(self, raw: dict, *, temporary: bool = False) -> dict:
        """转换一个旧消息图片或临时背景图片，保留缺图的可解释位置。

        Args:
            raw (dict): 原始图片对象。
            temporary (bool): 图片是否采用临时背景的平铺字段结构。
        Returns:
            dict: 新图片对象，或压缩旧图缺失时的文字块。
        Raises:
            MigrationError: 图片编码无效或缺少可转换的图片来源。
            ValueError: 图片引用或媒体类别无效。
        """
        image = raw if temporary else raw.get("image_url", {})
        url = image.get("url")
        kind = raw.get("media_kind", MediaKind.HUMAN)
        if kind == "screenshot":
            kind = MediaKind.TOOL
        kind = MediaKind(kind)
        reference = raw.get("storage_ref")
        if not reference and isinstance(url, str) and not url.startswith("data:"):
            reference = url
        if reference:
            reference = reference.replace("\\", "/")
            if reference.startswith("screenshot/"):
                reference = "tool/" + reference.removeprefix("screenshot/")
            resolve_image_path(reference, images_dir=self.root)
        # 1. 已有文件引用优先；只有缺少文件时才用旧记录中的内联编码补齐。
        if isinstance(url, str) and url.startswith("data:") and (
                not reference or (reference not in self.targets and not (self.root / reference).is_file())):
            header, separator, encoded = url.partition(",")
            extensions = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif",
                          "image/webp": "webp", "image/bmp": "bmp"}
            mime = header.removeprefix("data:").removesuffix(";base64")
            if not separator or not header.endswith(";base64") or mime not in extensions:
                raise MigrationError("内联图片格式无效")
            try:
                data = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise MigrationError("内联图片编码无效") from exc
            if not data:
                raise MigrationError("内联图片内容为空")
            if not reference:
                filename = f"{hashlib.sha256(data).hexdigest()}.{extensions[mime]}"
                reference = filename if kind is MediaKind.HUMAN else f"{kind.value}/{filename}"
            self.stage(reference, data)
        if not isinstance(reference, str) or not reference:
            if raw.get("context_state") == "compacted":
                self.missing.add("(旧图片缺少文件引用)")
                return TextContent("[图片不可用：旧图片缺少文件引用]").to_dict()
            raise MigrationError("图片缺少可转换的本地文件引用")
        path = resolve_image_path(reference, images_dir=self.root)
        available = reference in self.targets or path.is_file()
        if not available:
            self.missing.add(reference)
            if raw.get("context_state") == "compacted":
                return TextContent(f"[图片不可用：{reference}]").to_dict()
        elif raw.get("context_state") == "compacted":
            self.restored += 1
        converted = ImageContent(storage_ref=reference, media_kind=kind, detail=image.get("detail"))
        if temporary:
            result = {"storage_ref": reference, "media_kind": converted.media_kind.value}
            if converted.detail is not None:
                result["detail"] = converted.detail
            return result
        return converted.to_dict()

    def convert_message(self, raw: dict) -> dict:
        """只转换消息正文中的图片，保留工具参数和供应商恢复材料。

        Args:
            raw (dict): 已有的业务消息 JSON。
        Returns:
            dict: 图片转换后的消息副本。
        """
        result = deepcopy(raw)
        if isinstance(result.get("content"), list):
            result["content"] = [self.convert_image(part) if part.get("type") == "image_url" else part
                                 for part in result["content"]]
        return result

    def convert_checkpoint(self, raw: dict) -> dict:
        """转换父子会话快照及临时背景，并由新版状态契约校验恢复材料。

        Args:
            raw (dict): v3 或已迁移的 v4 checkpoint。
        Returns:
            dict: 可供新版状态恢复的 checkpoint。
        Raises:
            MigrationError: checkpoint 使用本工具不支持的版本。
        """
        if raw.get("schema_version") not in (3, CHECKPOINT_SCHEMA_VERSION):
            raise MigrationError("仅支持 v3 到 v4 的图片迁移")
        result = deepcopy(raw)
        result["messages"] = [self.convert_message(message) for message in raw["messages"]]
        for item in result.get("temporary_context", {}).values():
            pictures = []
            for image in item.get("images", []):
                converted = self.convert_image(image, temporary=True)
                if converted.get("type") == "text":
                    item["text"] = (item.get("text", "") + "\n" + converted["text"]).strip()
                else:
                    pictures.append(converted)
            item["images"] = pictures
        result["schema_version"] = CHECKPOINT_SCHEMA_VERSION
        validate_legacy_checkpoint(result)
        return result

    def write_targets(self) -> int:
        """写出已核验的图片副本，已经存在的相同文件直接复用。

        Returns:
            int: 本次实际新建的文件数量。
        Raises:
            MigrationError: 写入前发现目标文件内容冲突。
            OSError: 文件写入或发布失败，事务由调用方回滚。
        """
        copied = 0
        for reference, data in self.targets.items():
            target = resolve_image_path(reference, images_dir=self.root)
            if target.exists():
                if target.read_bytes() != data:
                    raise MigrationError(f"目标图片内容冲突: {reference}")
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
            try:
                with temporary.open("xb") as stream:
                    stream.write(data)
                temporary.rename(target)
            finally:
                temporary.unlink(missing_ok=True)
            copied += 1
        return copied

    def cleanup_sources(self) -> list[str]:
        """数据库提交后核验目标文件，再清理旧截图并报告未完成项。

        Returns:
            list[str]: 文件清理失败时保留的旧截图引用。
        Raises:
            MigrationError: 提交后目标文件内容与迁移计划不同。
        """
        remaining = []
        for source, reference in self.sources.items():
            target = resolve_image_path(reference, images_dir=self.root)
            if target.read_bytes() != self.targets[reference]:
                raise MigrationError(f"迁移后图片内容不一致: {reference}")
            try:
                (self.root / source).unlink()
            except OSError:
                remaining.append(source)
        directory = self.root / "screenshot"
        if directory.is_dir() and not any(directory.iterdir()):
            try:
                directory.rmdir()
            except OSError:
                remaining.append("screenshot/")
        return remaining


def _validate_written_records(db: sqlite3.Connection) -> None:
    """在提交版本之前读取事务内的实际记录，确认全部状态与历史可恢复。

    Args:
        db (sqlite3.Connection): 已写入转换结果、尚未提交的迁移事务。
    """
    for scope in AgentStorageScope:
        for (payload,) in db.execute(f"SELECT state_json FROM {scope.checkpoints_table}"):
            validate_legacy_checkpoint(json.loads(payload))
        for payload, kind, message_type, message_id, created_at in db.execute(
                f"SELECT message_json,record_kind,message_type,message_id,created_at FROM {scope.messages_table}"):
            if kind != LEGACY_TOOL_NOTICE_KIND:
                message_from_history(message_json=json.loads(payload), message_type=message_type,
                                     message_id=message_id, created_at=created_at)


def migrate(db_path: Path, images_dir: Path, *, apply: bool = False) -> MigrationReport:
    """检查或执行共享主库的图片引用迁移，四表与版本在同一事务内更新。

    Args:
        db_path (Path): 当前应用共享主库，调用者应先停止应用。
        images_dir (Path): 与该数据库对应的图片根目录。
        apply (bool): 是否执行文件复制、数据库更新和旧文件清理。
    Returns:
        MigrationReport: 不包含聊天正文的统计、文件映射和完整性结果。
    Raises:
        MigrationError: 版本、内容、文件冲突或完整性校验失败。
        sqlite3.Error: 无法取得数据库写入权限或事务执行失败。
        OSError: 图片文件读取或写入失败。
    """
    files = _ImageFiles(images_dir.resolve())
    with closing(sqlite3.connect(db_path.resolve().as_uri() + ("?mode=rw" if apply else "?mode=ro"),
                                uri=True, timeout=0.1)) as db:
        db.execute("PRAGMA foreign_keys=ON")
        if not apply:
            db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN IMMEDIATE" if apply else "BEGIN")
        try:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (3, CHECKPOINT_SCHEMA_VERSION):
                raise MigrationError(f"数据库版本 {version} 不支持本次迁移")
            # 1. 准备文件和全部 JSON 转换；检查通过之前保持数据库及图片目录原样。
            files.collect_screenshots()
            updates: list[tuple[str, str, int, str]] = []
            counts: dict[str, int] = {}
            for scope in AgentStorageScope:
                table = scope.checkpoints_table
                rows = db.execute(f"SELECT id,state_json FROM {table}").fetchall()
                counts[table] = len(rows)
                for row_id, payload in rows:
                    original = json.loads(payload)
                    converted = files.convert_checkpoint(original)
                    if original != converted:
                        updates.append((table, "state_json", row_id, json.dumps(converted, ensure_ascii=False)))
                table = scope.messages_table
                rows = db.execute(f"SELECT id,message_json,record_kind,message_type,message_id,created_at FROM {table}").fetchall()
                counts[table] = len(rows)
                for row_id, payload, kind, message_type, message_id, created_at in rows:
                    if kind == LEGACY_TOOL_NOTICE_KIND:
                        continue
                    original = json.loads(payload)
                    converted = files.convert_message(original)
                    message_from_history(message_json=converted, message_type=message_type,
                                         message_id=message_id, created_at=created_at)
                    if original != converted:
                        updates.append((table, "message_json", row_id, json.dumps(converted, ensure_ascii=False)))
            legacy = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='chat_history'").fetchone() is not None
            # 2. 图片先写出，四表更新、旧存档表删除及版本升级在同一数据库事务完成。
            copied = files.write_targets() if apply else 0
            if apply:
                for table, column, row_id, payload in updates:
                    db.execute(f"UPDATE {table} SET {column}=? WHERE id=?", (payload, row_id))
                if legacy:
                    db.execute("DROP TABLE chat_history")
                _validate_written_records(db)
            integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok" or db.execute("PRAGMA foreign_key_check").fetchall():
                raise MigrationError("数据库完整性校验失败")
            if apply:
                if version != CHECKPOINT_SCHEMA_VERSION:
                    db.execute(f"PRAGMA user_version={CHECKPOINT_SCHEMA_VERSION}")
                db.commit()
            else:
                db.rollback()
        except BaseException:
            db.rollback()
            raise
    # 3. 提交后核对文件，再清理旧副本；未完成清理可以在下次执行继续处理。
    remaining = files.cleanup_sources() if apply else sorted(files.sources)
    return MigrationReport(source_version=version, target_version=CHECKPOINT_SCHEMA_VERSION,
        table_rows=counts, changed_rows=len(updates), restored_images=files.restored,
        missing_refs=sorted(files.missing), files_copied=copied, file_mapping=dict(files.sources),
        dropped_legacy_history=bool(apply and legacy), remaining_source_files=remaining,
        applied=apply, integrity=integrity)


def main() -> None:
    """解析离线迁移参数并将统计报告写到标准输出。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path, help="共享主库路径")
    parser.add_argument("--images-dir", required=True, type=Path, help="对应图片根目录")
    parser.add_argument("--apply", action="store_true", help="确认停机后执行迁移")
    args = parser.parse_args()
    print(json.dumps(migrate(args.db, args.images_dir, apply=args.apply), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
