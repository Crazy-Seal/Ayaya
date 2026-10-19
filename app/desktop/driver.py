"""桌面控制器与平台驱动之间的进程内接口，不向控制器暴露原生窗口句柄。"""

from contextlib import AbstractContextManager
from typing import Callable, Protocol

from app.schemas.desktop import DesktopWindowRegistration, ScreenAction, ScreenCapture


class DesktopDriver(Protocol):
    """封装平台输入、窗口登记和恢复，全部方法由控制器的同一工作线程调用。"""

    def register_windows(self, registration: DesktopWindowRegistration) -> dict:
        """验证窗口身份并更新登记，返回接收结果和崩溃恢复文件位置。

        Args:
            registration (DesktopWindowRegistration): Electron 提供的窗口身份快照。
        Returns:
            dict: 是否接受快照，以及 Electron 使用的恢复文件路径。
        """
        ...

    def window_guard(self, *, hide: bool, check: Callable[[], None]) -> AbstractContextManager[None]:
        """在平台窗口准备完成后进入操作，退出时恢复本次隐藏的窗口。

        Args:
            hide (bool): 当前设置是否要求隐藏应用窗口。
            check (Callable[[], None]): 控制器提供的停止检查，恢复时不调用。
        Returns:
            AbstractContextManager[None]: 包含失败清理和崩溃恢复记录维护的操作范围。
        """
        ...

    def input_guard(self, text: str) -> AbstractContextManager[None]:
        """准备本批次输入资源，退出时释放按键并恢复受保护的剪贴板。

        Args:
            text (str): 非空表示本批次需要输入文字。
        Returns:
            AbstractContextManager[None]: 允许执行鼠标和键盘输入的范围。
        """
        ...

    def capture(self) -> ScreenCapture:
        """捕获主屏并返回编码后的观察。

        Returns:
            ScreenCapture: 图片内容和输出尺寸。
        """
        ...

    def mouse_action(self, action: ScreenAction, check: Callable[[], None]) -> None:
        """按平台坐标语义执行鼠标输入，在安全点检查停止。

        Args:
            action (ScreenAction): 已校验的位置比例与鼠标动作。
            check (Callable[[], None]): 控制器提供的停止检查。
        """
        ...

    def paste(self, text: str, check: Callable[[], None]) -> None:
        """通过平台输入机制粘贴文字，在安全点检查停止。

        Args:
            text (str): 当前动作需要输入的文字。
            check (Callable[[], None]): 控制器提供的停止检查。
        """
        ...

    def press_enter(self) -> None:
        """向当前获得焦点的窗口发送回车。"""
        ...

    def close(self) -> None:
        """在创建驱动的线程中释放全部原生资源。"""
        ...
