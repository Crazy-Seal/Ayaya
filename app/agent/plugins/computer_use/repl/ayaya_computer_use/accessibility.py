"""通过窗口所属的 UI Automation 控件读取信息和执行语义动作。"""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

from .types import AxNode, AxState

if TYPE_CHECKING:
    from uiautomation import Control

PATTERN_ACTIONS = {
    "Invoke": ("InvokePattern", "Invoke"),
    "Toggle": ("TogglePattern", "Toggle"),
    "Select": ("SelectionItemPattern", "Select"),
    "Expand": ("ExpandCollapsePattern", "Expand"),
    "Collapse": ("ExpandCollapsePattern", "Collapse"),
}


class Accessibility:
    """保存一个准确窗口的最近控件引用，重新观察后旧编号失效。"""

    def __init__(self, hwnd: int, indices: Iterator[int]) -> None:
        """绑定准确窗口，控件信息在显式观察时取得。

        Args:
            hwnd (int): 已验证目标窗口句柄。
            indices (Iterator[int]): 所属 cua 的单调编号，区分各窗口对象的引用。
        """
        self.hwnd = hwnd
        self._indices = indices
        self._controls: dict[int, Control] = {}

    def observe(self) -> AxState:
        """读取本窗口的控件树及可用操作，替换上一次引用。

        Returns:
            AxState: 可供模型选择控件编号的最新信息。
        """
        import uiautomation as auto
        self._controls.clear()
        nodes: list[AxNode] = []
        root = auto.ControlFromHandle(self.hwnd)
        if root is None:
            raise ValueError("窗口的控件树不可用，请重新列举窗口")
        pending: list[tuple[Control, int | None]] = [(root, None)]
        while pending:
            control, parent = pending.pop()
            index = next(self._indices)
            self._controls[index] = control
            rectangle = control.BoundingRectangle
            actions = tuple(name for name, (pattern, _) in PATTERN_ACTIONS.items()
                            if control.GetPattern(getattr(auto.PatternId, pattern)) is not None)
            value = control.GetPattern(auto.PatternId.ValuePattern)
            text = ""
            if not control.IsPassword:
                if value is not None:
                    text = value.Value
                    if not value.IsReadOnly:
                        actions += ("SetValue",)
                else:
                    pattern = control.GetPattern(auto.PatternId.TextPattern)
                    if pattern is not None:
                        text = pattern.DocumentRange.GetText(-1)
            nodes.append(AxNode(index, parent, control.Name, control.ControlTypeName,
                (rectangle.left, rectangle.top, rectangle.right, rectangle.bottom),
                control.IsEnabled, control.HasKeyboardFocus, text, actions))
            pending.extend((child, index) for child in reversed(control.GetChildren()))
        return AxState(tuple(nodes))

    def control(self, index: int) -> Control:
        """取得最新观察中的存活控件，并确认它仍属于绑定窗口。

        Args:
            index (int): 本次观察中的控件编号。

        Returns:
            Control: 当前仍可使用的 UIA 控件。

        Raises:
            ValueError: 旧编号、失效控件或不可操作目标。
        """
        import uiautomation as auto
        control = self._controls.get(index)
        if control is None:
            raise ValueError("控件编号已失效，请重新 get_ax_state()；请参考 cua.help()")
        root = auto.ControlFromHandle(self.hwnd)
        if root is None:
            raise ValueError("窗口已经失效，请重新列举窗口")
        current = control
        while current is not None and not auto.ControlsAreSame(current, root):
            current = current.GetParentControl()
        if current is None or not control.IsEnabled:
            raise ValueError("控件已离开目标窗口或不可操作，请重新观察")
        return control

    def set_value(self, index: int, value: str) -> None:
        """使用可写 ValuePattern 设置控件内容。

        Args:
            index (int): 当前控件编号。
            value (str): 需要设置的完整内容。
        """
        import uiautomation as auto
        pattern = self.control(index).GetPattern(auto.PatternId.ValuePattern)
        if pattern is None or pattern.IsReadOnly:
            raise ValueError("控件不支持设置值，请参考 cua.help() 选择其他操作")
        pattern.SetValue(value)

    def perform(self, index: int, action: str) -> None:
        """执行控件实际支持的命名动作。

        Args:
            index (int): 当前控件编号。
            action (str): Invoke、Toggle、Select、Expand 或 Collapse。
        """
        if action not in PATTERN_ACTIONS:
            raise ValueError("未知语义动作，请参考 cua.help()")
        import uiautomation as auto
        pattern_name, method = PATTERN_ACTIONS[action]
        pattern = self.control(index).GetPattern(getattr(auto.PatternId, pattern_name))
        if pattern is None:
            raise ValueError("控件不支持此语义动作，请重新观察可用动作")
        getattr(pattern, method)()

    def point(self, index: int) -> tuple[int, int]:
        """取得控件可点击点，系统未提供时使用控件当前边界中心。

        Args:
            index (int): 当前控件编号。

        Returns:
            tuple[int, int]: 控件当前桌面物理坐标。
        """
        control = self.control(index)
        x, y, found = control.GetClickablePoint()
        if found:
            return x, y
        rectangle = control.BoundingRectangle
        if rectangle.width() <= 0 or rectangle.height() <= 0 or control.IsOffscreen:
            raise ValueError("控件没有可点击位置，请重新观察")
        return rectangle.xcenter(), rectangle.ycenter()
