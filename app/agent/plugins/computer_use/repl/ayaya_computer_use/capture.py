"""捕获主屏幕或准确窗口，返回编码图片及桌面物理范围。"""

from collections.abc import Iterator
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
from io import BytesIO
import threading

from PIL import Image, ImageGrab

from .types import CaptureRegion, Screenshot

MAX_CAPTURE_SIZE = (1920, 1080)
CAPTURE_TIMEOUT_SECONDS = 5.0


@contextmanager
def physical_pixels() -> Iterator[None]:
    """在当前线程读取桌面物理像素，结束后恢复调用线程原有 DPI 设置。

    Yields:
        None: 截图与窗口测量使用相同物理坐标的范围。
    """
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
    user.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
    previous = user.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
    if not previous:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        yield
    finally:
        user.SetThreadDpiAwarenessContext(previous)


def encode_image(image: Image.Image, region: CaptureRegion) -> Screenshot:
    """等比缩小捕获结果，保留与原始物理区域对应的 PNG。

    Args:
        image (Image.Image): 本次捕获的像素。
        region (CaptureRegion): 缩小前的桌面物理区域。

    Returns:
        Screenshot: PNG 编码、图片尺寸和原始区域。
    """
    with image.copy() as scaled, BytesIO() as output:
        scaled.thumbnail(MAX_CAPTURE_SIZE, Image.Resampling.LANCZOS)
        scaled.save(output, format="PNG")
        return Screenshot(output.getvalue(), region, scaled.width, scaled.height)


def desktop_region() -> CaptureRegion:
    """读取主屏幕物理范围，供模型主屏坐标输入核对。

    Returns:
        CaptureRegion: 主屏幕原点、尺寸和物理坐标标记。
    """
    with physical_pixels():
        user = ctypes.WinDLL("user32")
        return CaptureRegion(0, 0, user.GetSystemMetrics(0), user.GetSystemMetrics(1), user.GetDpiForSystem())


def capture_desktop() -> Screenshot:
    """捕获包含透明窗口的主屏幕，保持所有应用窗口当前显隐。

    Returns:
        Screenshot: 当前主屏 PNG 和物理范围。
    """
    with physical_pixels(), ImageGrab.grab(include_layered_windows=True, all_screens=False) as image:
        return encode_image(image, desktop_region())


def window_region(hwnd: int) -> CaptureRegion:
    """读取 WGC 窗口画面对应的可见框架物理区域。

    Args:
        hwnd (int): 已核对的目标窗口句柄。

    Returns:
        CaptureRegion: DWM 可见边界与当前窗口 DPI。
    """
    with physical_pixels():
        rectangle = wintypes.RECT()
        dwm = ctypes.WinDLL("dwmapi")
        dwm.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
        result = dwm.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(rectangle), ctypes.sizeof(rectangle))
        if result < 0:
            raise OSError(f"无法读取窗口捕获边界：{result}")
        user = ctypes.WinDLL("user32")
        user.GetDpiForWindow.argtypes = [wintypes.HWND]
        return CaptureRegion(rectangle.left, rectangle.top, rectangle.right - rectangle.left,
                             rectangle.bottom - rectangle.top, user.GetDpiForWindow(hwnd))


def capture_window(hwnd: int) -> Screenshot:
    """通过 HWND 捕获窗口首帧，在所有退出路径停止捕获线程。

    Args:
        hwnd (int): 准确目标窗口句柄。

    Returns:
        Screenshot: 单窗口 PNG 及已核对的物理映射。

    Raises:
        RuntimeError: 未获得首帧、窗口在捕获时变化或边界无法对应。
    """
    from windows_capture import Frame, InternalCaptureControl, WindowsCapture
    before = window_region(hwnd)
    capture = WindowsCapture(window_hwnd=hwnd, cursor_capture=False, draw_border=False)
    arrived = threading.Event()
    frames: list[Image.Image] = []

    @capture.event
    def on_frame_arrived(frame: Frame, capture_control: InternalCaptureControl) -> None:
        """在回调有效期内复制首帧像素并请求停止捕获。"""
        pixels = frame.frame_buffer.copy()
        frames.append(Image.frombytes("RGBA", (frame.width, frame.height), pixels.tobytes(), "raw", "BGRA").convert("RGB"))
        capture_control.stop()
        arrived.set()

    @capture.event
    def on_closed() -> None:
        """窗口关闭时结束等待，由调用方报告缺失的画面。"""
        arrived.set()

    control = capture.start_free_threaded()
    try:
        if not arrived.wait(CAPTURE_TIMEOUT_SECONDS) or not frames:
            raise RuntimeError("未取得窗口首帧，请确认窗口可捕获后重新观察")
        control.wait()
        after = window_region(hwnd)
        image = frames[0]
        if before != after or image.size != (after.width, after.height):
            raise RuntimeError("窗口捕获范围已变化或无法核对边界，请重新观察")
        return encode_image(image, after)
    finally:
        control.stop()
        for image in frames:
            image.close()
