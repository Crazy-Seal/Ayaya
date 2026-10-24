"""桌面设置、窗口身份及进程内屏幕操作的明确数据契约。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Callable, Protocol

from pydantic import BaseModel, ConfigDict, Field, StrictBool

StopRequested = Callable[[], bool]


class ScreenOperation(StrEnum):
    """区分后端可以发出的鼠标动作。

    Attributes:
        CLICK (ScreenOperation): 单击。
        DOUBLE (ScreenOperation): 双击。
        RIGHT (ScreenOperation): 右键。
        SCROLL (ScreenOperation): 滚动。
    """

    CLICK = "click"
    DOUBLE = "double"
    RIGHT = "right"
    SCROLL = "scroll"


class ScrollDirection(StrEnum):
    """表示动作请求的滚动方向。

    Attributes:
        NONE (ScrollDirection): 请求中未指定滚动方向。
        DOWN (ScrollDirection): 向下滚动。
        UP (ScrollDirection): 向上滚动。
    """

    NONE = "none"
    DOWN = "down"
    UP = "up"


class ScreenActionStatus(StrEnum):
    """记录屏幕输入已经产生的实际执行程度。

    Attributes:
        NOT_STARTED (ScreenActionStatus): 尚未发出输入。
        PARTIAL_OR_UNKNOWN (ScreenActionStatus): 部分执行或执行程度未知。
        COMPLETED (ScreenActionStatus): 全部动作已经完成。
    """

    NOT_STARTED = "not_started"
    PARTIAL_OR_UNKNOWN = "partial_or_unknown"
    COMPLETED = "completed"


class DesktopSettings(BaseModel):
    """保存全部会话共享的屏幕显隐选项。

    Attributes:
        hide_on_screenshot (bool): 截图及操作期间是否临时隐藏桌宠和聊天窗口。
    """
    model_config = ConfigDict(extra="forbid")
    hide_on_screenshot: StrictBool = True


class DesktopWindows(BaseModel):
    """限定可被后端隐藏的两个窗口，十进制字符串避免 JS 大整数截断。

    Attributes:
        pet (str | None): 桌宠原生 HWND，未创建或已销毁时为空。
        chat (str | None): 独立聊天原生 HWND，最小化时仍登记。
    """
    model_config = ConfigDict(extra="forbid")
    pet: str | None = Field(default=None, pattern=r"^[1-9][0-9]{0,19}$")
    chat: str | None = Field(default=None, pattern=r"^[1-9][0-9]{0,19}$")


class DesktopWindowRegistration(BaseModel):
    """Electron 主进程发送的窗口完整快照，revision 在同一所有者中递增。

    Attributes:
        owner_pid (int): 实际持有 HWND 的 Electron 主进程编号，用于验证归属。
        revision (int): 同一主进程递增的完整快照版本。
        windows (DesktopWindows): 两个精确窗口角色，不包含其他同进程窗口。
    """
    model_config = ConfigDict(extra="forbid")
    owner_pid: int = Field(gt=0, le=0xFFFFFFFF)
    revision: int = Field(ge=0)
    windows: DesktopWindows


@dataclass(frozen=True)
class ScreenCapture:
    """返回模型观察图片，width/height 为编码后图片尺寸。

    Attributes:
        data_url (str): PNG 图像的 data URL。
        width (int): 等比例缩小后的输出宽度。
        height (int): 等比例缩小后的输出高度。
    """
    data_url: str
    width: int
    height: int


@dataclass(frozen=True)
class ScreenAction:
    """携带已由工具校验的动作，坐标比例对应主显示器物理像素。

    Attributes:
        x_ratio (float): 主屏水平位置比例，范围 0 到 1。
        y_ratio (float): 主屏垂直位置比例，范围 0 到 1。
        operation (ScreenOperation): 单击、双击、右键或滚动的具名操作。
        text (str): 鼠标操作后粘贴的文本，空串跳过输入。
        press_enter (bool): 文本粘贴完成后是否回车。
        wait_seconds (int): 动作后等待页面更新的秒数。
        scroll_direction (ScrollDirection): 滚动方向 up/down。
    """
    x_ratio: float
    y_ratio: float
    operation: ScreenOperation
    text: str = ""
    press_enter: bool = True
    wait_seconds: int = 3
    scroll_direction: ScrollDirection = ScrollDirection.DOWN


@dataclass(frozen=True)
class ScreenActionResult:
    """分别记录输入副作用和后续截图，错误不抹去已经发生的动作。

    Attributes:
        status (ScreenActionStatus): 未开始、部分或不确定、已完成的副作用状态。
        screenshot (ScreenCapture | None): 动作后的观察，截图失败时为空。
        error (str | None): 输入、捕获或清理失败说明，不代表动作一定未执行。
        completed_actions (int): 已完整发出鼠标和键盘输入的动作数量，不包含执行程度未知的当前动作。
        total_actions (int): 本批次请求的动作总数。
    """
    status: ScreenActionStatus
    screenshot: ScreenCapture | None = None
    error: str | None = None
    completed_actions: int = 0
    total_actions: int = 0


class DesktopOperations(Protocol):
    """工具依赖的截图与整批操作接口，生产与隔离操作系统测试共用此契约。"""

    async def capture_screen(self, *, stop_requested: StopRequested | None = None) -> ScreenCapture:
        """取得主屏观察，结束前恢复本次临时隐藏的窗口。

        Args:
            stop_requested (StopRequested | None): 当前轮次的只读停止检查。
        Returns:
            ScreenCapture: 已完成清理的主屏 PNG 图片及输出尺寸。
        """
        ...

    async def perform_actions(self, actions: tuple[ScreenAction, ...], *, stop_requested: StopRequested | None = None) -> ScreenActionResult:
        """在同一锁及窗口隐藏期间顺序执行整批动作，最后截图，失败时停止余下动作。

        Args:
            actions (tuple[ScreenAction, ...]): 已由 Pipeline 整批获准的有序动作及物理位置比例。
            stop_requested (StopRequested | None): 当前轮次的只读停止检查。
        Returns:
            ScreenActionResult: 如实区分执行程度、完成数量、最终观察和错误。
        """
        ...
