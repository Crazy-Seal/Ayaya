"""按中英文近似信息密度计算并截断文本。"""


WEIGHTED_TEXT_TRUNCATED_MARKER = "\n\n[工具结果过长，已截断中间部分]\n\n"


def char_weight(char: str) -> float:
    """返回单个 Unicode code point 的文本权重。

    Args:
        char (str): 需要计算显示长度权重的单个字符。
    """
    return 0.25 if ord(char) < 128 else 1.0


def weighted_char_count(value: str) -> float:
    """计算文本的加权字符数。

    Args:
        value (str): 当前需要校验或转换的值。
    """
    return sum(char_weight(char) for char in value)


def _take_weighted_prefix(value: str, budget: float) -> str:
    """从文本开头按字符权重保留内容，不超过指定加权长度预算。

    Args:
        value (str): 当前需要校验或转换的值。
        budget (float): 允许保留的加权字符数。

    Returns:
        str: 完整原文或符合预算的前段文本。
    """
    used = 0.0
    end = 0
    for end, char in enumerate(value, start=1):
        weight = char_weight(char)
        if used + weight > budget:
            return value[: end - 1]
        used += weight
    return value[:end]


def _take_weighted_suffix(value: str, budget: float) -> str:
    """从文本末尾按字符权重保留内容，供超长工具结果展示尾部信息。

    Args:
        value (str): 当前需要校验或转换的值。
        budget (float): 允许保留的加权字符数。

    Returns:
        str: 完整原文或符合预算的末段文本。
    """
    used = 0.0
    start = len(value)
    for start in range(len(value) - 1, -1, -1):
        weight = char_weight(value[start])
        if used + weight > budget:
            return value[start + 1 :]
        used += weight
    return value[start:]


def truncate_weighted_text(
    value: str,
    *,
    max_weight: float = 10000,
    head_weight: float = 8000,
    tail_weight: float = 2000,
    marker: str = WEIGHTED_TEXT_TRUNCATED_MARKER,
) -> str:
    """按权重保留原文头尾，结构提示不占原文额度。

    Args:
        value (str): 当前需要校验或转换的值。
        max_weight (float): 输出最多保留的加权字符总数。
        head_weight (float): 截断时分配给原文开头的加权字符预算。
        tail_weight (float): 截断时分配给原文结尾的加权字符预算。
        marker (str): 截断时插入的省略、外置引用或读取范围提示。
    """
    if head_weight + tail_weight > max_weight:
        raise ValueError("头尾权重之和不能超过总权重")
    if weighted_char_count(value) <= max_weight:
        return value
    return (
        _take_weighted_prefix(value, head_weight)
        + marker
        + _take_weighted_suffix(value, tail_weight)
    )


__all__ = ["truncate_weighted_text", "weighted_char_count"]
