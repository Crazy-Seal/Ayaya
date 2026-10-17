"""由隔离 Electron 验证脚本启动，真实桌面输入仅使用指定测试窗口。"""

import asyncio
import ctypes
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.schemas.desktop import DesktopSettings, DesktopWindowRegistration, ScreenAction
from app.desktop.desktop_controller import DesktopController
from app.desktop.windows_desktop_driver import WindowsDesktopDriver


async def main() -> None:
    """读入隔离窗口身份，在真实系统 adapter 上记录捕获时的可见状态。"""
    request = json.loads(sys.stdin.read())
    observed = []

    class ObservedWindows(WindowsDesktopDriver):
        """沿用生产 Win32 行为，仅在截图前记录窗口状态用于断言。"""

        def capture(self):
            """记录真实窗口可见状态后调用生产截图，不替换系统行为。

            Returns:
                ScreenCapture: 已编码的真实主屏图片与输出尺寸。
            """
            observed.append({role: {"visible": self.visible(int(hwnd)), "minimized": self.minimized(int(hwnd))}
                             for role, hwnd in request["observed"].items()})
            if request.get("crash"):
                # 1. 只在隔离验收中模拟清理前进程死亡，留下真实 journal 供 Electron 兜底。
                print(json.dumps({"observed": observed, "backend_pid": os.getpid()}), flush=True)
                os._exit(23)
            return super().capture()

    class ProbeController(DesktopController):
        """仍在生产工作线程创建 adapter，仅增加截图时的真实状态观察。"""

        def _native(self):
            """原生对象必须在唯一工作线程初始化 DPI。

            Returns:
                ObservedWindows: 此线程唯一的真实 Windows adapter。
            """
            if self._adapter is None:
                self._adapter = ObservedWindows()
            return self._adapter

    desktop = ProbeController(lambda: DesktopSettings(hide_on_screenshot=request.get("hide", True)),
                              owner_pid=request["owner_pid"])
    desktop.start()
    try:
        await desktop.register_windows(DesktopWindowRegistration(owner_pid=request["owner_pid"], revision=1,
                                                                windows=request["windows"]))
        if request.get("target"):
            def point():
                """使用独立测试窗口的物理矩形中心，避免把 DIP 再乘 DPI。

                Returns:
                    tuple[float, float]: 测试窗口中心相对于主屏物理尺寸的比例。
                """
                native = desktop._adapter
                rect = native._w.RECT()
                native._user.GetWindowRect.argtypes = [native._w.HWND, ctypes.POINTER(native._w.RECT)]
                native._user.GetWindowRect.restype = native._w.BOOL
                assert native._user.GetWindowRect(int(request["target"]), ctypes.byref(rect))
                return ((rect.left + rect.right) / 2 / native._user.GetSystemMetrics(0),
                        (rect.top + rect.bottom) / 2 / native._user.GetSystemMetrics(1))
            x, y = await asyncio.get_running_loop().run_in_executor(desktop._executor, point)
            result = await desktop.perform_action(ScreenAction(x, y, "click", text=request.get("text", ""), press_enter=False,
                                                               wait_seconds=0.2))
            image = result.screenshot
            output = {"status": result.status, "error": result.error}
        else:
            image = await desktop.capture_screen()
            output = {}
        output.update({"observed": observed, "size": [image.width, image.height] if image else None})
        print(json.dumps(output, ensure_ascii=True))
    finally:
        await desktop.close()


if __name__ == "__main__":
    asyncio.run(main())
