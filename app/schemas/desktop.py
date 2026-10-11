"""桌面设置、窗口身份及进程内屏幕操作的明确数据契约。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, StrictBool

StopRequested = Callable[[], bool]


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
        operation (Literal): 单击、双击、右键或滚动的具名操作。
        text (str): 鼠标操作后粘贴的文本，空串跳过输入。
        press_enter (bool): 文本粘贴完成后是否回车。
        wait_seconds (int): 动作后等待页面更新的秒数。
        scroll_direction (str): 滚动方向 up/down。
    """
    x_ratio: float
    y_ratio: float
    operation: Literal["click", "double", "right", "scroll"]
    text: str = ""
    press_enter: bool = True
    wait_seconds: int = 3
    scroll_direction: str = "down"


@dataclass(frozen=True)
class ScreenActionResult:
    """分别记录输入副作用和后续截图，错误不抹去已经发生的动作。

    Attributes:
        status (Literal): 未开始、部分或不确定、已完成的副作用状态。
        screenshot (ScreenCapture | None): 动作后的观察，截图失败时为空。
        error (str | None): 输入、捕获或清理失败说明，不代表动作一定未执行。
    """
    status: Literal["not_started", "partial_or_unknown", "completed"]
    screenshot: ScreenCapture | None = None
    error: str | None = None


class DesktopOperations(Protocol):
    """工具依赖的两个桌面接口，生产与隔离操作系统测试共用此契约。"""

    async def capture_screen(self, *, stop_requested: StopRequested | None = None) -> ScreenCapture:
        """取得主屏观察，结束前恢复本次临时隐藏的窗口。

        Args:
            stop_requested (StopRequested | None): 当前轮次的只读停止检查。
        Returns:
            ScreenCapture: 已完成清理的主屏 PNG 图片及输出尺寸。
        """
        ...

    async def perform_action(self, action: ScreenAction, *, stop_requested: StopRequested | None = None) -> ScreenActionResult:
        """串行执行已获准动作，清理后返回真实副作用及可选截图。

        Args:
            action (ScreenAction): 已由 Pipeline 获准的动作及物理位置比例。
            stop_requested (StopRequested | None): 当前轮次的只读停止检查。
        Returns:
            ScreenActionResult: 如实区分副作用状态、观察图片和错误。
        """
        ...
