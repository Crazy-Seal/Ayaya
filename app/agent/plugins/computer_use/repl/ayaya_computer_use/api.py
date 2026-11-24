"""向模型代码提供已绑定 host 的屏幕能力入口。"""

from __future__ import annotations

from pathlib import Path
from collections.abc import Callable
from functools import wraps
from inspect import signature
from itertools import count
from typing import TYPE_CHECKING, NoReturn, ParamSpec, TypeVar

from .types import AxState, Screenshot, WindowInfo
from .accessibility import Accessibility, PATTERN_ACTIONS

if TYPE_CHECKING:
    from repl_api import JsonValue, ReplHost
    from .screen_helper import ScreenHelper

T = TypeVar("T")
P = ParamSpec("P")


def api_method(function: Callable[P, T]) -> Callable[P, T]:
    """让公开方法的参数绑定错误附带帮助入口。

    Args:
        function (Callable[P, T]): 保留原始签名的模型可调用方法。

    Returns:
        Callable[P, T]: 参数错误时提示读取帮助的方法。
    """
    parameters = signature(function)

    @wraps(function)
    def invoke(*args: P.args, **kwargs: P.kwargs) -> T:
        """校验公开调用的参数名称，随后执行原方法。"""
        try:
            parameters.bind(*args, **kwargs)
        except TypeError as exc:
            raise TypeError(f"{exc}；请参考 cua.help()") from exc
        return function(*args, **kwargs)
    return invoke


class Cua:
    """协调当前 Python 调用中的观察、桌面操作和控制资源。"""

    def __getattr__(self, name: str) -> NoReturn:
        """未知公开属性或方法指向完整帮助。

        Args:
            name (str): 模型请求的属性或方法名。

        Raises:
            AttributeError: 属性名称不属于当前能力接口。
        """
        raise AttributeError(f"未知屏幕能力 {name}，请参考 cua.help()")

    def __init__(self, host: ReplHost, *, desktop: ScreenHelper | None = None) -> None:
        """保存 runner 的输出和执行控制接口，实际操作时再取得桌面资源。

        Args:
            host (ReplHost): runner 为本能力提供的公共接口。
            desktop (ScreenHelper | None): 默认延迟创建的本机桌面依赖，测试可替换系统边界。
        """
        self._host = host
        self._desktop = desktop
        self._screenshot: Screenshot | None = None
        self._control_id = ""
        self._element_indices = count(1)

    def _read(self, observe: Callable[[], T]) -> T:
        """在原生观察前后交付当前执行已受理的停止。

        Args:
            observe (Callable[[], T]): 读取窗口身份、控件或画面的系统操作。

        Returns:
            T: 未被中断的观察结果。
        """
        execution_id = self._host.execution_id
        self._host.raise_if_interrupted(execution_id)
        try:
            return observe()
        finally:
            self._host.raise_if_interrupted(execution_id)

    def _begin_control(self) -> None:
        """首次控制前登记收尾、开启物理 ESC 监听并发布本次提示。"""
        execution_id = self._host.execution_id
        self._host.raise_if_interrupted(execution_id)
        if self._control_id == execution_id:
            return
        listener = self.desktop.create_escape_listener(lambda: self._host.request_interrupt(execution_id))

        def cleanup() -> None:
            """结束本次监听和输入状态，保留能力对象供下一调用使用。"""
            try:
                listener.close()
            finally:
                try:
                    self.desktop.release_inputs()
                finally:
                    if self._control_id == execution_id:
                        self._control_id = ""

        self._host.defer_cleanup(execution_id, cleanup)
        listener.start()
        self._host.publish_event(execution_id, "status_notice", {
            "id": f"{execution_id}.screen", "active": True, "text": "正在控制屏幕，按 ESC 退出",
        })
        self._control_id = execution_id

    def _act(self, window: WindowInfo | None, action: Callable[[], None], *, activate: bool = True) -> str:
        """在明确目标和执行边界内完成一次桌面动作。

        Args:
            window (WindowInfo | None): 窗口输入目标，主屏幕动作保持当前前台窗口。
            action (Callable[[], None]): 已校验参数的系统动作。
            activate (bool): 是否需要在真实输入前激活目标。

        Returns:
            str: 提醒 Agent 主动观察结果的动作回执。
        """
        self._begin_control()
        execution_id = self._host.execution_id
        self._host.raise_if_interrupted(execution_id)
        if window is not None:
            self.desktop.verify(window)
            if activate:
                self.desktop.activate(window)
        try:
            action()
        finally:
            self._host.raise_if_interrupted(execution_id)
        result = "桌面动作已执行，请按任务需要主动观察结果。"
        print(result)
        return result

    def _point(self, window: WindowInfo | None, screenshot: Screenshot | None,
               x: int | None, y: int | None) -> tuple[int, int]:
        """把最近截图中的坐标转换到当前物理位置。

        Args:
            window (WindowInfo | None): 窗口目标或主屏幕。
            screenshot (Screenshot | None): 最近一次捕获的区域依据。
            x (int | None): 归一化横坐标。
            y (int | None): 归一化纵坐标。

        Returns:
            tuple[int, int]: 桌面物理像素坐标。

        Raises:
            ValueError: 缺少截图、坐标错误或窗口尺寸及 DPI 已变化。
        """
        if type(x) is not int or type(y) is not int or not 0 <= x <= 1000 or not 0 <= y <= 1000:
            raise ValueError("x、y 必须为 0～1000 的整数；请参考 cua.help()")
        if screenshot is None:
            raise ValueError("请先重新截图取得坐标映射；请参考 cua.help()")
        current = self.desktop.get_region(window)
        previous = screenshot.region
        if (current.width, current.height, current.dpi) != (previous.width, previous.height, previous.dpi):
            raise ValueError("窗口尺寸或 DPI 已变化，请重新截图")
        return (current.left + min(current.width - 1, round(x / 1000 * current.width)),
                current.top + min(current.height - 1, round(y / 1000 * current.height)))

    def _click(self, window: WindowInfo | None, screenshot: Screenshot | None, *,
               element_index: int | None, x: int | None, y: int | None, button: str, clicks: int,
               controls: Accessibility | None = None) -> str:
        """校验单一定位方式后执行鼠标点击。

        Args:
            window (WindowInfo | None): 已绑定目标或主屏幕。
            screenshot (Screenshot | None): 当前坐标映射。
            element_index (int | None): 最新观察中的控件编号。
            controls (Accessibility | None): 所属窗口对象保存的控件引用。
            x (int | None): 截图横坐标。
            y (int | None): 截图纵坐标。
            button (str): left、right 或 middle。
            clicks (int): 单击或双击。

        Returns:
            str: 已提交输入的回执。
        """
        if button not in {"left", "right", "middle"} or type(clicks) is not int or clicks not in (1, 2):
            raise ValueError("button 或 clicks 无效；请参考 cua.help()")
        if element_index is not None:
            if window is None or x is not None or y is not None or type(element_index) is not int:
                raise ValueError("控件编号和坐标只能选择一种；请参考 cua.help()")

        def action() -> None:
            """激活后再次确认点击位置仍属于目标，再发送实际鼠标输入。"""
            point = (self.desktop.click_point(window, controls, element_index)
                     if window is not None and element_index is not None and controls is not None
                     else self._point(window, screenshot, x, y))
            self.desktop.ensure_point(window, point)
            self.desktop.click(point, button, clicks)
        return self._act(window, action)

    @api_method
    def click(self, *, x: int, y: int, button: str = "left", clicks: int = 1) -> str:
        """点击最近主屏截图上的归一化位置。

        Args:
            x (int): 0～1000 横坐标。
            y (int): 0～1000 纵坐标。
            button (str): 鼠标按钮。
            clicks (int): 单击为 1，双击为 2。

        Returns:
            str: 动作执行回执。
        """
        return self._click(None, self._screenshot, element_index=None, x=x, y=y, button=button, clicks=clicks)

    @property
    def desktop(self) -> ScreenHelper:
        """在首次需要实际桌面能力时创建系统资源。

        Returns:
            ScreenHelper: 当前 REPL 主线程使用的桌面实现。
        """
        if self._desktop is None:
            from .screen_helper import ScreenHelper
            self._desktop = ScreenHelper()
        return self._desktop

    @api_method
    def get_app(self, *, window_id: str) -> Window:
        """绑定窗口清单中的准确窗口编号。

        Args:
            window_id (str): list_windows 返回的准确窗口编号，使用十进制字符串。

        Returns:
            Window: 保留窗口身份及最近观察的操作对象。

        Raises:
            ValueError: 窗口编号的类型或格式无效，或对应窗口已经失效。
        """
        return Window(self, self._read(lambda: self.desktop.window(window_id)))

    def _keys(self, window: WindowInfo | None, keys: tuple[str, ...]) -> str:
        """在目标激活后发送组合键，并在输入中检查停止。

        Args:
            window (WindowInfo | None): 指定目标或当前前台窗口。
            keys (tuple[str, ...]): 按下并释放的按键序列。

        Returns:
            str: 输入回执。
        """
        if not keys or any(not isinstance(key, str) or not key for key in keys):
            raise ValueError("按键必须为非空字符串；请参考 cua.help()")
        return self._act(window, lambda: self.desktop.keyboard(keys,
            lambda: self._host.raise_if_interrupted(self._host.execution_id)))

    def _text(self, window: WindowInfo | None, text: str) -> str:
        """向指定窗口输入 Unicode，实际输入与剪贴板恢复由系统边界处理。

        Args:
            window (WindowInfo | None): 指定目标或当前前台窗口。
            text (str): 需要输入的文字。

        Returns:
            str: 输入回执。
        """
        if not isinstance(text, str):
            raise ValueError("text 必须为字符串；请参考 cua.help()")
        return self._act(window, lambda: self.desktop.type_text(text,
            lambda: self._host.raise_if_interrupted(self._host.execution_id)))

    def _scroll(self, window: WindowInfo | None, screenshot: Screenshot | None, *,
                element_index: int | None, x: int | None, y: int | None, direction: str, amount: int,
                controls: Accessibility | None = None) -> str:
        """在控件位置或截图坐标上滚动指定格数。

        Args:
            window (WindowInfo | None): 指定目标或主屏幕。
            screenshot (Screenshot | None): 最近坐标映射。
            element_index (int | None): 当前控件编号。
            controls (Accessibility | None): 所属窗口对象保存的控件引用。
            x (int | None): 截图横坐标。
            y (int | None): 截图纵坐标。
            direction (str): up 或 down。
            amount (int): 正整数滚轮格数。

        Returns:
            str: 滚动回执。
        """
        if direction not in {"up", "down"} or type(amount) is not int or amount <= 0:
            raise ValueError("滚动方向或格数无效；请参考 cua.help()")
        if element_index is not None:
            if window is None or x is not None or y is not None or type(element_index) is not int:
                raise ValueError("控件编号和坐标只能选择一种；请参考 cua.help()")

        def action() -> None:
            """核对滚轮目标后发送一次滚动输入。"""
            point = (self.desktop.click_point(window, controls, element_index)
                     if window is not None and element_index is not None and controls is not None
                     else self._point(window, screenshot, x, y))
            self.desktop.ensure_point(window, point)
            self.desktop.scroll(point, amount if direction == "up" else -amount)
        return self._act(window, action)

    def _drag(self, window: WindowInfo | None, screenshot: Screenshot | None,
              start_x: int, start_y: int, end_x: int, end_y: int) -> str:
        """把同一截图内的起终点转换成一次拖动。

        Args:
            window (WindowInfo | None): 指定目标或主屏幕。
            screenshot (Screenshot | None): 最近坐标映射。
            start_x (int): 起点横坐标。
            start_y (int): 起点纵坐标。
            end_x (int): 终点横坐标。
            end_y (int): 终点纵坐标。

        Returns:
            str: 拖动回执。
        """
        def action() -> None:
            """激活后重新换算两端目标，再执行可清理的鼠标拖动。"""
            start = self._point(window, screenshot, start_x, start_y)
            end = self._point(window, screenshot, end_x, end_y)
            self.desktop.ensure_point(window, start)
            self.desktop.ensure_point(window, end)
            self.desktop.drag(start, end, lambda: self._host.raise_if_interrupted(self._host.execution_id))
        return self._act(window, action)

    @api_method
    def type_text(self, text: str) -> str:
        """向当前前台目标输入文字。

        Args:
            text (str): Unicode 文本。

        Returns:
            str: 输入回执。
        """
        return self._text(None, text)

    @api_method
    def press_key(self, key: str) -> str:
        """向当前前台目标发送一个按键。

        Args:
            key (str): 按键名称。

        Returns:
            str: 输入回执。
        """
        return self._keys(None, (key,))

    @api_method
    def hotkey(self, *keys: str) -> str:
        """向当前前台目标发送组合键。

        Args:
            *keys (str): 按下顺序的按键名称。

        Returns:
            str: 输入回执。
        """
        return self._keys(None, keys)

    @api_method
    def scroll(self, *, x: int, y: int, direction: str, amount: int = 3) -> str:
        """在主屏截图坐标处滚动。

        Args:
            x (int): 归一化横坐标。
            y (int): 归一化纵坐标。
            direction (str): up 或 down。
            amount (int): 正整数滚轮格数。

        Returns:
            str: 滚动回执。
        """
        return self._scroll(None, self._screenshot, element_index=None, x=x, y=y, direction=direction, amount=amount)

    @api_method
    def drag(self, *, start_x: int, start_y: int, end_x: int, end_y: int) -> str:
        """在最近主屏截图内拖动。

        Args:
            start_x (int): 起点横坐标。
            start_y (int): 起点纵坐标。
            end_x (int): 终点横坐标。
            end_y (int): 终点纵坐标。

        Returns:
            str: 拖动回执。
        """
        return self._drag(None, self._screenshot, start_x, start_y, end_x, end_y)

    @api_method
    def list_windows(self) -> list[WindowInfo]:
        """返回并输出当前可见窗口清单。

        Returns:
            list[WindowInfo]: 可供 get_app 使用的窗口身份。
        """
        windows = self._read(self.desktop.list_windows)
        for window in windows:
            print(f"window_id={window.window_id} pid={window.pid} "
                  f"process_name={window.process_name} title={window.title}")
        return windows

    @api_method
    def get_screenshot(self) -> Screenshot:
        """输出主屏幕画面并保存对应的坐标映射。

        Returns:
            Screenshot: 主屏幕编码图片和物理捕获范围。
        """
        screenshot = self._read(self.desktop.screenshot)
        self._screenshot = screenshot
        self._print_capture(screenshot)
        self._host.emit_image(screenshot.image)
        return screenshot

    @staticmethod
    def _print_capture(screenshot: Screenshot) -> None:
        """输出截图的物理捕获范围和展示尺寸，供模型理解坐标映射。

        Args:
            screenshot (Screenshot): 即将输出的图片及捕获信息。
        """
        print(f"capture_region={screenshot.region} image_size=({screenshot.width}, {screenshot.height})")

    @api_method
    def help(self) -> None:
        """打印插件包中的完整使用说明，供 Agent 首次操作前阅读。"""
        print((Path(__file__).parents[2] / "docs" / "api.md").read_text(encoding="utf-8"))


class Window:
    """将窗口观察和后续动作限定到绑定时确认的目标。"""

    def __getattr__(self, name: str) -> NoReturn:
        """未知窗口方法指向完整帮助。

        Args:
            name (str): 模型请求的属性或方法名。

        Raises:
            AttributeError: 属性名称不属于窗口接口。
        """
        raise AttributeError(f"未知窗口能力 {name}，请参考 cua.help()")

    def __init__(self, cua: Cua, identity: WindowInfo) -> None:
        """保存所属能力和准确窗口身份。

        Args:
            cua (Cua): 共享执行控制资源的能力对象。
            identity (WindowInfo): 绑定时读取的窗口与进程身份。
        """
        self._cua = cua
        self._identity = identity
        self._controls = Accessibility(int(identity.window_id), cua._element_indices)
        self._screenshot: Screenshot | None = None

    @api_method
    def type_text(self, text: str) -> str:
        """激活绑定窗口后输入 Unicode 文本。

        Args:
            text (str): 输入内容。

        Returns:
            str: 输入回执。
        """
        return self._cua._text(self._identity, text)

    @api_method
    def press_key(self, key: str) -> str:
        """激活绑定窗口后发送单键。

        Args:
            key (str): 按键名称。

        Returns:
            str: 输入回执。
        """
        return self._cua._keys(self._identity, (key,))

    @api_method
    def hotkey(self, *keys: str) -> str:
        """激活绑定窗口后发送组合键。

        Args:
            *keys (str): 按下顺序的按键名称。

        Returns:
            str: 输入回执。
        """
        return self._cua._keys(self._identity, keys)

    @api_method
    def scroll(self, *, element_index: int | None = None, x: int | None = None,
               y: int | None = None, direction: str, amount: int = 3) -> str:
        """在绑定窗口的控件位置或截图坐标处滚动。

        Args:
            element_index (int | None): 当前控件编号。
            x (int | None): 归一化横坐标。
            y (int | None): 归一化纵坐标。
            direction (str): up 或 down。
            amount (int): 正整数滚轮格数。

        Returns:
            str: 滚动回执。
        """
        return self._cua._scroll(self._identity, self._screenshot, element_index=element_index,
                                 x=x, y=y, direction=direction, amount=amount, controls=self._controls)

    @api_method
    def drag(self, *, start_x: int, start_y: int, end_x: int, end_y: int) -> str:
        """在最近窗口截图内拖动。

        Args:
            start_x (int): 起点横坐标。
            start_y (int): 起点纵坐标。
            end_x (int): 终点横坐标。
            end_y (int): 终点纵坐标。

        Returns:
            str: 拖动回执。
        """
        return self._cua._drag(self._identity, self._screenshot, start_x, start_y, end_x, end_y)

    @api_method
    def set_value(self, *, element_index: int, value: str) -> str:
        """使用最近观察的控件引用设置文本，失败时由 Agent 决定后续动作。

        Args:
            element_index (int): 最新控件编号。
            value (str): 输入控件的完整内容。

        Returns:
            str: 语义动作执行回执。
        """
        if type(element_index) is not int or not isinstance(value, str):
            raise ValueError("控件编号或文本类型无效；请参考 cua.help()")
        return self._cua._act(self._identity,
            lambda: self._cua.desktop.set_value(self._identity, self._controls, element_index, value), activate=False)

    @api_method
    def perform_secondary_action(self, *, element_index: int, action: str) -> str:
        """执行最近观察明确列出的控件命名动作。

        Args:
            element_index (int): 最新控件编号。
            action (str): Invoke、Toggle、Select、Expand 或 Collapse。

        Returns:
            str: 语义动作执行回执。
        """
        if type(element_index) is not int or action not in PATTERN_ACTIONS:
            raise ValueError("控件编号或语义动作无效；请参考 cua.help()")
        return self._cua._act(self._identity,
            lambda: self._cua.desktop.perform(self._identity, self._controls, element_index, action), activate=False)

    @api_method
    def click(self, *, element_index: int | None = None, x: int | None = None,
              y: int | None = None, button: str = "left", clicks: int = 1) -> str:
        """按控件位置或最近窗口截图中的坐标点击。

        Args:
            element_index (int | None): 当前控件编号。
            x (int | None): 0～1000 横坐标。
            y (int | None): 0～1000 纵坐标。
            button (str): 鼠标按钮。
            clicks (int): 单击为 1，双击为 2。

        Returns:
            str: 动作执行回执。
        """
        return self._cua._click(self._identity, self._screenshot, element_index=element_index,
                               x=x, y=y, button=button, clicks=clicks, controls=self._controls)

    @api_method
    def get_ax_state(self) -> AxState:
        """取得并输出本窗口的最新控件编号和支持的动作。

        Returns:
            AxState: 本次观察中的控件信息。
        """
        state = self._cua._read(lambda: self._cua.desktop.ax_state(self._identity, self._controls))
        for node in state.nodes:
            print(f"[{node.element_index}] parent={node.parent_index} {node.role} {node.name!r} "
                  f"enabled={node.enabled} focused={node.focused} bounds={node.bounds} "
                  f"actions={node.actions} text={node.text!r}")
        return state

    @api_method
    def get_screenshot(self) -> Screenshot:
        """输出当前绑定窗口的画面并保存坐标映射。

        Returns:
            Screenshot: 本次窗口编码图片和物理捕获范围。
        """
        screenshot = self._cua._read(lambda: self._cua.desktop.screenshot(self._identity))
        self._screenshot = screenshot
        self._cua._print_capture(screenshot)
        self._cua._host.emit_image(screenshot.image)
        return screenshot


def create(config: dict[str, JsonValue], host: ReplHost) -> Cua:
    """由 runner 预装屏幕能力对象。

    Args:
        config (dict[str, JsonValue]): 本能力当前使用空初始化配置。
        host (ReplHost): runner 提供的公共能力接口。

    Returns:
        Cua: 模型命名空间中的 cua 对象。
    """
    return Cua(host)
