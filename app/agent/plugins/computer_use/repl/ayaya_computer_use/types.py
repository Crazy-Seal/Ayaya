"""保存窗口观察、捕获区域和可供模型使用的控件信息。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class WindowInfo:
    """标识准确窗口及其所属进程，供后续观察和输入核对目标。"""

    window_id: str
    title: str
    pid: int
    process_name: str


@dataclass(frozen=True)
class CaptureRegion:
    """记录截图对应的桌面物理范围及 DPI，供坐标换算使用。"""

    left: int
    top: int
    width: int
    height: int
    dpi: int


@dataclass(frozen=True)
class Screenshot:
    """向代码返回编码图片和实际捕获范围，图片尺寸可因缩放而不同。"""

    image: bytes
    region: CaptureRegion
    width: int
    height: int


@dataclass(frozen=True)
class AxNode:
    """描述本次可访问性观察中的一个控件及其支持的语义动作。"""

    element_index: int
    parent_index: int | None
    name: str
    role: str
    bounds: tuple[int, int, int, int]
    enabled: bool
    focused: bool
    text: str
    actions: tuple[str, ...]


@dataclass(frozen=True)
class AxState:
    """向模型提供最新控件编号、层级和可操作信息。"""

    nodes: tuple[AxNode, ...]


@dataclass(frozen=True)
class WindowState:
    """返回同一个窗口的一次控件和图片联合观察。"""

    ax_state: AxState
    screenshot: Screenshot
