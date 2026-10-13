from app.agent.state.messages import ImageContent
import base64
from pathlib import Path

from app.agent.tools.base import ToolResult
from app.agent.tools.result_artifacts import ToolResultArtifactStore
from app.agent.tools.result_normalizer import (
    TOOL_RESULT_TOO_LARGE_MARKER,
    TOOL_RESULT_TRUNCATED_MARKER,
    ToolResultNormalizer,
    truncate_weighted_text,
    weighted_char_count,
)


def test_weighted_text_limit_treats_ascii_as_quarter_character() -> None:
    """验证ASCII 按四分之一加权字符计数。"""
    assert weighted_char_count("a" * 20_000) == 5_000
    assert weighted_char_count("汉" * 5_000) == 5_000
    assert truncate_weighted_text("a" * 40_000) == "a" * 40_000
    assert truncate_weighted_text("汉" * 10_000) == "汉" * 10_000


def test_weighted_text_truncation_preserves_head_and_tail_budgets() -> None:
    """验证加权截断分别保留首尾预算。"""
    value = "头" * 8_001 + "middle" + "尾" * 2_001

    normalized = truncate_weighted_text(value)

    head, tail = normalized.split(TOOL_RESULT_TRUNCATED_MARKER)
    assert head.rstrip("\n") == "头" * 8_000
    assert tail.lstrip("\n") == "尾" * 2_000


def test_memory_copy_can_use_800_plus_200_weighted_limit() -> None:
    """验证记忆副本支持八百加二百加权字符预算。"""
    value = "前" * 900 + "middle" + "后" * 300

    normalized = truncate_weighted_text(
        value,
        max_weight=1_000,
        head_weight=800,
        tail_weight=200,
    )

    head, tail = normalized.split(TOOL_RESULT_TRUNCATED_MARKER)
    assert head.rstrip("\n") == "前" * 800
    assert tail.lstrip("\n") == "后" * 200


def test_builtin_images_share_strict_mcp_validation() -> None:
    """验证内置图片与 MCP 图片使用相同严格校验。"""
    png = b"\x89PNG\r\n\x1a\n" + b"payload"
    valid = "data:image/png;base64," + base64.b64encode(png).decode()
    invalid_signature = "data:image/png;base64," + base64.b64encode(b"not-png").decode()
    bare_base64 = base64.b64encode(png).decode()

    result = ToolResult.success(
        "ok",
        image_urls=[valid, invalid_signature, bare_base64],
    )
    normalized = ToolResultNormalizer().normalize(result)

    assert [part.url for part in normalized.content if isinstance(part, ImageContent)] == [valid]
    assert "文件签名不匹配" in normalized.text
    assert "必须使用完整 data URL" in normalized.text


def test_oversized_result_is_saved_and_preview_references_artifact(tmp_path: Path) -> None:
    """验证超长结果保存全文并在预览中给出引用。

    Args:
        tmp_path (Path): Pytest 为当前用例创建的临时目录，不访问生产存储。
    """
    store = ToolResultArtifactStore(tmp_path / "tool_results")
    normalizer = ToolResultNormalizer(artifact_store=store)
    original = "头" * 8_001 + "中间" * 2_000 + "尾" * 2_001

    normalized = normalizer.normalize(
        ToolResult.success(original),
        session_id="session-a",
        persist_overflow=True,
    )

    assert "头" * 8_000 in normalized.text
    assert "尾" * 2_000 in normalized.text
    assert "tool-result://session-a/" in normalized.text
    uri = normalized.text.split("tool-result://", 1)[1].split("；", 1)[0]
    artifact = store.resolve("session-a", f"tool-result://{uri}")
    assert artifact.read_text(encoding="utf-8") == original


def test_read_result_preview_does_not_create_another_artifact(tmp_path: Path) -> None:
    """验证读取结果预览不生成新的外置结果。

    Args:
        tmp_path (Path): Pytest 为当前用例创建的临时目录，不访问生产存储。
    """
    store = ToolResultArtifactStore(tmp_path / "tool_results")
    normalizer = ToolResultNormalizer(artifact_store=store)

    normalized = normalizer.normalize(
        ToolResult.success("内容" * 10_001),
        session_id="session-a",
        persist_overflow=False,
    )

    assert "请缩小读取范围后重试" in normalized.text
    assert not (tmp_path / "tool_results").exists()


def test_result_over_10_mib_is_not_saved(tmp_path: Path) -> None:
    """验证超过十 MiB 的结果不保存全文。

    Args:
        tmp_path (Path): Pytest 为当前用例创建的临时目录，不访问生产存储。
    """
    store = ToolResultArtifactStore(tmp_path / "tool_results")
    normalizer = ToolResultNormalizer(artifact_store=store)

    normalized = normalizer.normalize(
        ToolResult.success("x" * (10 * 1024 * 1024 + 1)),
        session_id="session-a",
        persist_overflow=True,
    )

    assert TOOL_RESULT_TOO_LARGE_MARKER.strip() in normalized.text
    assert not (tmp_path / "tool_results").exists()


def test_artifact_uri_is_scoped_to_current_session(tmp_path: Path) -> None:
    """验证工具结果引用只允许当前会话读取。

    Args:
        tmp_path (Path): Pytest 为当前用例创建的临时目录，不访问生产存储。
    """
    store = ToolResultArtifactStore(tmp_path / "tool_results")
    reference = store.save("session-a", "secret")

    assert reference.path == tmp_path / "tool_results" / "session-a" / f"{reference.artifact_id}.txt"
    assert store.resolve("session-a", reference.uri) == reference.path

    try:
        store.resolve("session-b", reference.uri)
    except ValueError as exc:
        assert "不属于当前会话" in str(exc)
    else:
        raise AssertionError("跨会话 artifact URI 应被拒绝")


def test_image_diagnostics_are_included_before_text_budgeting(tmp_path: Path) -> None:
    """验证忽略图片的原因先加入正文再计算文本预算。

    Args:
        tmp_path (Path): Pytest 为当前用例创建的临时目录，不访问生产存储。
    """
    store = ToolResultArtifactStore(tmp_path / "tool_results")
    normalizer = ToolResultNormalizer(artifact_store=store)

    normalized = normalizer.normalize(
        ToolResult.success("汉" * 10_000, image_urls=["invalid"]),
        session_id="session-a",
        persist_overflow=True,
    )

    assert "tool-result://session-a/" in normalized.text
    uri = "tool-result://" + normalized.text.split("tool-result://", 1)[1].split("；", 1)[0]
    saved = store.resolve("session-a", uri).read_text(encoding="utf-8")
    assert saved.endswith("[已忽略 1 张工具图片：必须使用完整 data URL]")
