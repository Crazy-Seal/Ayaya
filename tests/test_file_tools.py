import asyncio
from pathlib import Path
from types import SimpleNamespace

from app.agent.tools.read_file import read_file
from app.agent.tools.read_image import read_image
from app.agent.tools.apply_patch import apply_patch
from app.agent.tools.result_artifacts import ToolResultArtifactStore
from app.agent.tools.result_normalizer import ToolResultNormalizer
from app.agent.state.messages import TOOL_IMAGE_MESSAGE_NAME
from app.runtime import get_tool_results_dir
from app.agent.tools.registry import ToolRegistry


def _context(session_id: str = "session-a") -> SimpleNamespace:
    return SimpleNamespace(session_id=session_id)


def test_read_file_supports_line_and_character_ranges(tmp_path: Path, monkeypatch) -> None:
    import app.agent.utils.infra.safe_path as safe_path_module

    monkeypatch.setattr(safe_path_module, "WORKDIR", tmp_path)
    (tmp_path / "sample.txt").write_text("甲行\n乙行\n丙行\n", encoding="utf-8")

    async def scenario() -> tuple[str, str]:
        lines = await read_file._tool_func(
            path="sample.txt", start=1, end=3, unit="line", context=_context()
        )
        chars = await read_file._tool_func(
            path="sample.txt", start=2, end=6, unit="character", context=_context()
        )
        return lines, chars

    lines, chars = asyncio.run(scenario())

    assert lines == "乙行\n丙行"
    assert chars == "\n乙行\n"


def test_read_file_resolves_current_session_artifact() -> None:
    store = ToolResultArtifactStore(get_tool_results_dir())
    reference = store.save("session-a", "第一行\n第二行\n第三行")

    result = asyncio.run(read_file._tool_func(
        path=reference.uri,
        start=1,
        end=2,
        unit="line",
        context=_context(),
    ))

    assert result == "第二行"


def test_read_file_rejects_invalid_range_and_cross_session_artifact() -> None:
    store = ToolResultArtifactStore(get_tool_results_dir())
    reference = store.save("session-a", "secret")

    invalid_range = asyncio.run(read_file._tool_func(
        path=reference.uri,
        start=2,
        end=1,
        unit="line",
        context=_context(),
    ))
    cross_session = asyncio.run(read_file._tool_func(
        path=reference.uri,
        start=None,
        end=None,
        unit="line",
        context=_context("session-b"),
    ))

    assert invalid_range.startswith("错误:")
    assert cross_session.startswith("错误:")


def test_read_file_disables_oversized_result_persistence() -> None:
    assert read_file.persist_oversized_result is False


def test_read_image_returns_valid_images_as_system_tool_images(tmp_path: Path, monkeypatch) -> None:
    import app.agent.utils.infra.safe_path as safe_path_module

    monkeypatch.setattr(safe_path_module, "WORKDIR", tmp_path)
    png = b"\x89PNG\r\n\x1a\n" + b"payload"
    (tmp_path / "valid.png").write_bytes(png)
    (tmp_path / "invalid.png").write_bytes(b"not-png")

    raw = asyncio.run(read_image._tool_func(paths=["valid.png", "invalid.png"]))
    normalized = ToolResultNormalizer().normalize(raw)

    assert normalized.image_message_name == TOOL_IMAGE_MESSAGE_NAME
    assert len(normalized.image_urls) == 1
    assert "文件签名不匹配" in normalized.content


def test_read_image_counts_only_valid_images_toward_limit(tmp_path: Path, monkeypatch) -> None:
    import app.agent.utils.infra.safe_path as safe_path_module

    monkeypatch.setattr(safe_path_module, "WORKDIR", tmp_path)
    for index in range(5):
        (tmp_path / f"invalid-{index}.png").write_bytes(b"not-png")
    valid = b"\x89PNG\r\n\x1a\n" + b"payload"
    (tmp_path / "valid.png").write_bytes(valid)

    raw = asyncio.run(read_image._tool_func(paths=[
        *(f"invalid-{index}.png" for index in range(5)),
        "valid.png",
    ]))
    normalized = ToolResultNormalizer().normalize(raw)

    assert len(normalized.image_urls) == 1
    assert normalized.image_urls[0].startswith("data:image/png;base64,")


def test_run_ps_preserves_output_for_unified_normalization(monkeypatch) -> None:
    import app.agent.tools.run_ps as run_ps_module

    class _Process:
        pid = 1

        async def communicate(self):
            return b"x" * 60_000, b""

    async def fake_build_command(_command: str):
        return ["fake"]

    async def fake_create_subprocess_exec(*_args, **_kwargs):
        return _Process()

    monkeypatch.setattr(run_ps_module, "_build_command", fake_build_command)
    monkeypatch.setattr(
        run_ps_module.asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )

    result = asyncio.run(run_ps_module._run_command("Write-Output x"))

    assert result == "x" * 60_000


def test_apply_patch_adds_updates_and_deletes_files(tmp_path: Path, monkeypatch) -> None:
    import app.agent.utils.infra.safe_path as safe_path_module

    monkeypatch.setattr(safe_path_module, "WORKDIR", tmp_path)
    (tmp_path / "update.txt").write_text("before\nkeep\n", encoding="utf-8")
    (tmp_path / "delete.txt").write_text("obsolete", encoding="utf-8")
    patch = """*** Begin Patch
*** Add File: added.txt
+new file
*** Update File: update.txt
@@
-before
+after
 keep
*** Delete File: delete.txt
*** End Patch"""

    result = asyncio.run(apply_patch._tool_func(patch=patch))

    assert result.startswith("已应用补丁")
    assert (tmp_path / "added.txt").read_text(encoding="utf-8") == "new file\n"
    assert (tmp_path / "update.txt").read_text(encoding="utf-8") == "after\nkeep\n"
    assert not (tmp_path / "delete.txt").exists()


def test_apply_patch_validates_every_operation_before_writing(tmp_path: Path, monkeypatch) -> None:
    import app.agent.utils.infra.safe_path as safe_path_module

    monkeypatch.setattr(safe_path_module, "WORKDIR", tmp_path)
    patch = """*** Begin Patch
*** Add File: should-not-exist.txt
+content
*** Update File: missing.txt
@@
-before
+after
*** End Patch"""

    result = asyncio.run(apply_patch._tool_func(patch=patch))

    assert result.startswith("错误:")
    assert not (tmp_path / "should-not-exist.txt").exists()


def test_apply_patch_rejects_workspace_escape(tmp_path: Path, monkeypatch) -> None:
    import app.agent.utils.infra.safe_path as safe_path_module

    monkeypatch.setattr(safe_path_module, "WORKDIR", tmp_path)
    patch = """*** Begin Patch
*** Add File: ../escape.txt
+content
*** End Patch"""

    result = asyncio.run(apply_patch._tool_func(patch=patch))

    assert result.startswith("错误:")
    assert not (tmp_path.parent / "escape.txt").exists()


def test_tool_registry_exposes_new_tools_without_internal_modules() -> None:
    names = ToolRegistry.list_tools()

    assert "apply_patch" in names
    assert "read_image" in names
    assert "write_file" not in names
    assert "edit_file" not in names
    assert "delete_file" not in names
    assert "result_artifacts" not in names
    assert "result_limits" not in names
    assert "result_normalizer" not in names


def test_apply_patch_schema_explains_the_supported_patch_grammar() -> None:
    description = apply_patch.parameters_schema["properties"]["patch"]["description"]

    assert "*** Add File:" in description
    assert "*** Update File:" in description
    assert "*** Delete File:" in description
    assert "@@" in description
    assert "同一路径在一个补丁中只能操作一次" in description
    assert "原文和上下文必须在文件中唯一匹配" in description
    assert "*** Add File: notes/new.txt\n+第一行" in description
    assert (
        "*** Update File: app/example.py\n"
        "@@\n"
        " unchanged()\n"
        "-old_value = 1\n"
        "+old_value = 2\n"
        " unchanged()"
    ) in description
    assert "*** Delete File: notes/obsolete.txt" in description
