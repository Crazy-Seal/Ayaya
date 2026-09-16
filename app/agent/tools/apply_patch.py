"""apply_patch 工具 - 用一个补丁完成工作区文件的新增、修改和删除。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Literal

from app.agent.tools.decorator import tool
from app.agent.utils.infra.safe_path import safe_path


PatchKind = Literal["add", "update", "delete"]


@dataclass
class _Hunk:
    old_text: str
    new_text: str


@dataclass
class _Operation:
    kind: PatchKind
    path: str
    content: str = ""
    hunks: list[_Hunk] = field(default_factory=list)


_HEADERS = {
    "*** Add File: ": "add",
    "*** Update File: ": "update",
    "*** Delete File: ": "delete",
}


def _operation_header(line: str) -> tuple[PatchKind, str] | None:
    for prefix, kind in _HEADERS.items():
        if line.startswith(prefix):
            path = line[len(prefix):].strip()
            if not path:
                raise ValueError("补丁文件路径不能为空")
            return kind, path  # type: ignore[return-value]
    return None


def _parse_update_hunks(lines: list[str], start: int) -> tuple[list[_Hunk], int]:
    hunks: list[_Hunk] = []
    index = start
    while index < len(lines) - 1 and _operation_header(lines[index]) is None:
        if not lines[index].startswith("@@"):
            raise ValueError("Update File 必须使用 @@ 开始变更块")
        index += 1
        old_lines: list[str] = []
        new_lines: list[str] = []
        while index < len(lines) - 1:
            line = lines[index]
            if line.startswith("@@") or _operation_header(line) is not None:
                break
            if not line or line[0] not in {" ", "+", "-"}:
                raise ValueError("变更块中的每一行必须以空格、+ 或 - 开头")
            marker, value = line[0], line[1:]
            if marker in {" ", "-"}:
                old_lines.append(value)
            if marker in {" ", "+"}:
                new_lines.append(value)
            index += 1
        if not old_lines:
            raise ValueError("Update File 变更块必须包含原文或上下文")
        hunks.append(_Hunk("\n".join(old_lines), "\n".join(new_lines)))
    if not hunks:
        raise ValueError("Update File 至少需要一个变更块")
    return hunks, index


def _parse_patch(patch: str) -> list[_Operation]:
    lines = patch.splitlines()
    if len(lines) < 3 or lines[0] != "*** Begin Patch" or lines[-1] != "*** End Patch":
        raise ValueError("补丁必须由 *** Begin Patch 和 *** End Patch 包围")

    operations: list[_Operation] = []
    seen_paths: set[str] = set()
    index = 1
    while index < len(lines) - 1:
        header = _operation_header(lines[index])
        if header is None:
            raise ValueError(f"无法识别的补丁行: {lines[index]}")
        kind, path = header
        if path in seen_paths:
            raise ValueError(f"同一补丁不能重复操作文件: {path}")
        seen_paths.add(path)
        index += 1

        if kind == "add":
            content_lines: list[str] = []
            while index < len(lines) - 1 and _operation_header(lines[index]) is None:
                if not lines[index].startswith("+"):
                    raise ValueError("Add File 的内容行必须以 + 开头")
                content_lines.append(lines[index][1:])
                index += 1
            content = "\n".join(content_lines)
            if content_lines:
                content += "\n"
            operations.append(_Operation(kind=kind, path=path, content=content))
        elif kind == "update":
            hunks, index = _parse_update_hunks(lines, index)
            operations.append(_Operation(kind=kind, path=path, hunks=hunks))
        else:
            if index < len(lines) - 1 and _operation_header(lines[index]) is None:
                raise ValueError("Delete File 后不能包含内容")
            operations.append(_Operation(kind=kind, path=path))

    if not operations:
        raise ValueError("补丁中没有文件操作")
    return operations


def _prepare_changes(operations: list[_Operation]) -> dict[Path, str | None]:
    prepared: dict[Path, str | None] = {}
    for operation in operations:
        path = safe_path(operation.path)
        if operation.kind == "add":
            if path.exists():
                raise ValueError(f"文件已存在，无法新增: {operation.path}")
            prepared[path] = operation.content
            continue
        if not path.is_file():
            raise ValueError(f"文件不存在: {operation.path}")
        if operation.kind == "delete":
            prepared[path] = None
            continue

        content = path.read_text(encoding="utf-8-sig")
        for hunk in operation.hunks:
            count = content.count(hunk.old_text)
            if count == 0:
                raise ValueError(f"{operation.path} 中不存在补丁目标文本")
            if count > 1:
                raise ValueError(f"{operation.path} 中的补丁目标文本不唯一")
            content = content.replace(hunk.old_text, hunk.new_text, 1)
        prepared[path] = content
    return prepared


def _write_changes(prepared: dict[Path, str | None]) -> None:
    originals = {
        path: path.read_bytes() if path.exists() else None
        for path in prepared
    }
    try:
        for path, content in prepared.items():
            if content is None:
                path.unlink()
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
    except Exception:
        for path, original in originals.items():
            if original is None:
                path.unlink(missing_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(original)
        raise


@tool
async def apply_patch(
    patch: Annotated[
        str,
        "完整补丁文本。补丁必须由 *** Begin Patch 和 *** End Patch 包围。"
        "每个文件使用 *** Add File: <路径>、*** Update File: <路径> 或 "
        "*** Delete File: <路径>，同一路径在一个补丁中只能操作一次。"
        "Add File 的每行内容以 + 开头；Update File 的每个变更块以 @@ 开始，"
        "随后每行分别以空格（表示定位用的上下文）、-（表示删除）或 +（表示新增）开头，原文和上下文必须在文件中唯一匹配；"
        "Delete File 后不能有内容。使用本工具时，请使用相对路径，该相对路径会自动定位于你的工作区内\n\n"
        "示例：\n"
        "*** Begin Patch\n"
        "*** Add File: notes/new.txt\n"
        "+第一行\n"
        "*** Update File: app/example.py\n"
        "@@\n"
        " unchanged()\n"
        "-old_value = 1\n"
        "+old_value = 2\n"
        " unchanged()\n"
        "*** Delete File: notes/obsolete.txt\n"
        "*** End Patch",
    ],
) -> str:
    """使用一个补丁新增、修改或删除一个或多个工作区文件。"""
    try:
        operations = _parse_patch(patch)
        prepared = _prepare_changes(operations)
        _write_changes(prepared)
        added = sum(operation.kind == "add" for operation in operations)
        updated = sum(operation.kind == "update" for operation in operations)
        deleted = sum(operation.kind == "delete" for operation in operations)
        return f"已应用补丁：新增 {added}，修改 {updated}，删除 {deleted}。"
    except Exception as exc:
        return f"错误: {exc}"
