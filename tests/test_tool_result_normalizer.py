import base64

from app.agent.tools.base import ToolResult
from app.agent.tools.result_normalizer import (
    TOOL_RESULT_TRUNCATED_MARKER,
    ToolResultNormalizer,
    truncate_weighted_text,
    weighted_char_count,
)


def test_weighted_text_limit_treats_ascii_as_quarter_character() -> None:
    assert weighted_char_count("a" * 20_000) == 5_000
    assert weighted_char_count("汉" * 5_000) == 5_000
    assert truncate_weighted_text("a" * 40_000) == "a" * 40_000
    assert truncate_weighted_text("汉" * 10_000) == "汉" * 10_000


def test_weighted_text_truncation_preserves_head_and_tail_budgets() -> None:
    value = "头" * 8_001 + "middle" + "尾" * 2_001

    normalized = truncate_weighted_text(value)

    head, tail = normalized.split(TOOL_RESULT_TRUNCATED_MARKER)
    assert head.rstrip("\n") == "头" * 8_000
    assert tail.lstrip("\n") == "尾" * 2_000


def test_memory_copy_can_use_800_plus_200_weighted_limit() -> None:
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
    png = b"\x89PNG\r\n\x1a\n" + b"payload"
    valid = "data:image/png;base64," + base64.b64encode(png).decode()
    invalid_signature = "data:image/png;base64," + base64.b64encode(b"not-png").decode()
    bare_base64 = base64.b64encode(png).decode()

    result = ToolResult.success(
        "ok",
        image_urls=[valid, invalid_signature, bare_base64],
    )
    normalized = ToolResultNormalizer().normalize(result)

    assert normalized.image_urls == [valid]
    assert "文件签名不匹配" in normalized.content
    assert "必须使用完整 data URL" in normalized.content
