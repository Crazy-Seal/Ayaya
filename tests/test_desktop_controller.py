"""从桌面模块 interface 验证行为，操作系统 adapter 使用隔离替身。"""

import asyncio
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

from app.schemas.desktop import DesktopSettings, DesktopWindowRegistration, ScreenCapture, ScreenAction
from app.desktop.desktop_controller import DesktopController
from app.desktop.windows_desktop_driver import WindowsDesktopDriver


class FakeDesktop(WindowsDesktopDriver):
    """模拟窗口和屏幕的操作系统，不移动真实鼠标、不截图。"""

    def __init__(self, restore_path: Path, owner_pid: int = 0) -> None:
        """使用生产窗口管理方法，只替换系统窗口与输入调用。

        Args:
            restore_path (Path): 独立测试目录中的窗口恢复文件位置。
            owner_pid (int): 模拟 Electron 进程编号，零表示等待首次登记。
        """
        self._restore_path = restore_path
        self._owner_pid = owner_pid
        self._registration = None
        self.windows = {11: {"owner": 100, "visible": True, "minimized": False},
                        12: {"owner": 100, "visible": True, "minimized": False},
                        13: {"owner": 100, "visible": True, "minimized": False}}
        self.focus = 99
        self.observed = []
        self.actions = []
        self.fail_capture = False

    def window_owner(self, hwnd):
        """查询模拟操作系统保存的句柄归属。

        Args:
            hwnd (int): 测试操作系统或 Electron 登记的精确原生窗口句柄。
        """
        return self.windows.get(hwnd, {}).get("owner")

    def visible(self, hwnd):
        """查询当前窗口显隐，不把最小化当作隐藏。

        Args:
            hwnd (int): 测试操作系统或 Electron 登记的精确原生窗口句柄。
        """
        return self.windows[hwnd]["visible"]

    def minimized(self, hwnd):
        """查询窗口是否由用户最小化。

        Args:
            hwnd (int): 测试操作系统或 Electron 登记的精确原生窗口句柄。
        """
        return self.windows[hwnd]["minimized"]

    def hide(self, hwnd):
        """模拟系统隐藏窗口但保持对象、位置和输入焦点。

        Args:
            hwnd (int): 测试操作系统或 Electron 登记的精确原生窗口句柄。
        """
        self.windows[hwnd]["visible"] = False

    def show_inactive(self, hwnd):
        """模拟不激活窗口的恢复。

        Args:
            hwnd (int): 测试操作系统或 Electron 登记的精确原生窗口句柄。
        """
        self.windows[hwnd]["visible"] = True

    def capture(self):
        """记录实际观察到的窗口，不伪造已隐藏窗口。"""
        self.observed.append([hwnd for hwnd, state in self.windows.items() if state["visible"]])
        if self.fail_capture:
            raise OSError("截图失败")
        return ScreenCapture("data:image/png;base64,test", 1920, 1080)

    def close(self):
        """释放模拟系统资源，不改变其他窗口。"""
        pass

    @contextmanager
    def input_guard(self, _text):
        """输入清理由 Windows adapter 负责，此替身只让控制器测试观察执行程度。

        Args:
            _text (str): 本次输入文本，替身只模拟停止检查或清理范围。
        """
        yield

    def mouse_action(self, action, check):
        """记录系统收到的输入意图，测试不模拟任何真实点击。

        Args:
            action (ScreenAction): 已经由工具准备并获准的鼠标位置和输入配置。
            check (Callable[[], None]): 控制器提供的停止安全点，抛出异常时必须转入输入清理。
        """
        check()
        self.actions.append(action)

    def paste(self, _text, check):
        """模拟输入期间的停止安全点。

        Args:
            _text (str): 本次输入文本，替身只模拟停止检查或清理范围。
            check (Callable[[], None]): 控制器提供的停止安全点，抛出异常时必须转入输入清理。
        """
        check()

    def press_enter(self):
        """模拟回车已完成。"""
        pass


@pytest.mark.parametrize("failure", ["none", "second_action", "capture", "stop"])
def test_batch_actions_keep_one_window_guard_and_stop_after_failure(tmp_path, monkeypatch, failure) -> None:
    """验证整批只隐藏恢复一次、只在最后截图，并保留失败或停止前的真实进度。

    Args:
        tmp_path (Path): 测试窗口恢复记录的独立目录。
        monkeypatch (pytest.MonkeyPatch): 在动作间等待结束时注入停止通知。
        failure (str): 正常执行、第二步故障、最终截图故障或动作间停止。
    """
    class BatchDesktop(FakeDesktop):
        """记录输入准备和窗口显隐，不执行真实桌面操作。"""
        def __init__(self, restore_path):
            """初始化与本批次对应的显隐和剪贴板记录。

            Args:
                restore_path (Path): 当前测试的窗口恢复文件位置。
            """
            super().__init__(restore_path)
            self.hidden = []
            self.shown = []
            self.guards = []

        def hide(self, hwnd):
            """记录本批次隐藏的窗口。

            Args:
                hwnd (int): 模拟窗口句柄。
            """
            self.hidden.append(hwnd)
            super().hide(hwnd)

        def show_inactive(self, hwnd):
            """记录本批次恢复的窗口。

            Args:
                hwnd (int): 模拟窗口句柄。
            """
            self.shown.append(hwnd)
            super().show_inactive(hwnd)

        @contextmanager
        def input_guard(self, text):
            """记录整批输入预检查，结束时也记录清理。

            Args:
                text (str): 用于判断整批是否需要保存剪贴板的文本。

            Yields:
                None: 允许当前批次执行桌面输入。
            """
            self.guards.append(text)
            try:
                yield
            finally:
                self.guards.append("cleaned")

        def mouse_action(self, action, check):
            """记录输入，第二步可以模拟已经部分发送后的失败。

            Args:
                action (ScreenAction): 当前要执行的动作。
                check (Callable): 原控制器的停止检查。
            """
            assert not self.visible(11) and not self.visible(12)
            super().mouse_action(action, check)
            if failure == "second_action" and len(self.actions) == 2:
                raise OSError("第二步输入中断")

    async def scenario():
        """通过批量公开接口观察动作顺序、进度和最终清理。"""
        native = BatchDesktop(tmp_path / "restore.json")
        native.fail_capture = failure == "capture"
        desktop = DesktopController(lambda: DesktopSettings(), driver_factory=lambda: native)
        desktop.start()
        await desktop.register_windows(DesktopWindowRegistration(owner_pid=100, revision=1, windows={"pet": "11", "chat": "12"}))
        pause = desktop._pause

        def stop_after_first_wait(seconds, cancelled):
            """在第一步已经完成时触发公开关闭信号，阻止第二步开始。

            Args:
                seconds (float): 原流程需要等待的秒数。
                cancelled (threading.Event): 原流程接收的取消信号。
            """
            pause(seconds, cancelled)
            if failure == "stop" and len(native.actions) == 1:
                desktop.begin_shutdown()

        monkeypatch.setattr(desktop, "_pause", stop_after_first_wait)
        actions = (
            ScreenAction(0.2, 0.2, "click", wait_seconds=0),
            ScreenAction(0.5, 0.5, "click", text="第二步文字", press_enter=False, wait_seconds=0),
            ScreenAction(0.8, 0.8, "click", wait_seconds=0),
        )
        try:
            result = await desktop.perform_actions(actions)
            assert result.total_actions == 3
            expected_completed = 3 if failure in {"none", "capture"} else 1
            assert result.completed_actions == expected_completed
            assert result.status == ("completed" if expected_completed == 3 else "partial_or_unknown")
            expected_inputs = 2 if failure == "second_action" else 1 if failure == "stop" else 3
            assert native.actions == list(actions[:expected_inputs])
            assert len(native.observed) == (1 if expected_completed == 3 else 0)
            assert native.hidden == native.shown == [11, 12]
            assert native.guards == ["第二步文字", "cleaned"]
            assert native.visible(11) and native.visible(12)
            assert not (tmp_path / "restore.json").exists()
            assert bool(result.error) == (failure != "none")
        finally:
            await desktop.close()

    asyncio.run(scenario())


def test_invalid_later_action_prevents_entire_batch(tmp_path) -> None:
    """后面的无效坐标必须在第一次输入前被发现，不能产生部分批次。

    Args:
        tmp_path (Path): 测试窗口恢复记录的独立目录。
    """
    async def scenario():
        """给公开接口提供第二步无效的数组，观察动作和进度均为空。"""
        native = FakeDesktop(tmp_path / "restore.json")
        desktop = DesktopController(lambda: DesktopSettings(hide_on_screenshot=False), driver_factory=lambda: native)
        desktop.start()
        try:
            result = await desktop.perform_actions((
                ScreenAction(0.5, 0.5, "click", wait_seconds=0),
                ScreenAction(float("nan"), 0.5, "click", wait_seconds=0),
            ))
            assert result.status == "not_started" and result.completed_actions == 0
            assert result.total_actions == 2 and result.error
            assert not native.actions and not native.observed
        finally:
            await desktop.close()
    asyncio.run(scenario())


def test_capture_hides_only_registered_windows_and_restores_without_focus(tmp_path) -> None:
    """通过截图 interface 验证只有登记窗口消失且结束后恢复原焦点。

    Args:
        tmp_path (Path): Pytest 为本用例创建的独立目录，不访问生产存储。
    """
    async def scenario():
        """在独立控制器生命周期内登记两个窗口并观察截图。"""
        native = FakeDesktop(tmp_path / "restore.json")
        desktop = DesktopController(lambda: DesktopSettings(), driver_factory=lambda: native)
        desktop.start()
        await desktop.register_windows(DesktopWindowRegistration(owner_pid=100, revision=1, windows={"pet": "11", "chat": "12"}))
        try:
            image = await desktop.capture_screen()
            assert image.width == 1920
            assert native.observed == [[13]]
            assert all(state["visible"] for state in native.windows.values())
            assert native.focus == 99
            assert not (tmp_path / "restore.json").exists()
        finally:
            await desktop.close()
    asyncio.run(scenario())


def test_capture_failure_and_minimized_windows_preserve_original_visibility(tmp_path) -> None:
    """截图失败也恢复可见窗口，原本最小化的聊天不能弹出。

    Args:
        tmp_path (Path): Pytest 为本用例创建的独立目录，不访问生产存储。
    """
    async def scenario():
        """在观察失败前安排聊天最小化，并检查清理后的外部状态。"""
        import pytest
        native = FakeDesktop(tmp_path / "restore.json")
        native.windows[12]["minimized"] = True
        native.fail_capture = True
        desktop = DesktopController(lambda: DesktopSettings(), driver_factory=lambda: native)
        desktop.start()
        await desktop.register_windows(DesktopWindowRegistration(owner_pid=100, revision=2, windows={"pet": "11", "chat": "12"}))
        try:
            with pytest.raises(OSError):
                await desktop.capture_screen()
            assert native.observed == [[12, 13]]
            assert native.windows[11]["visible"]
            assert native.windows[12]["minimized"]
            assert not (tmp_path / "restore.json").exists()
        finally:
            await desktop.close()
    asyncio.run(scenario())


def test_completed_action_survives_post_action_capture_failure(tmp_path) -> None:
    """动作完成后截图异常必须仍返回 completed，不会误导重复点击。

    Args:
        tmp_path (Path): Pytest 为本用例创建的独立目录，不访问生产存储。
    """
    async def scenario():
        """从公共动作接口观察已执行的动作与捕获错误。"""
        native = FakeDesktop(tmp_path / "restore.json")
        native.fail_capture = True
        desktop = DesktopController(lambda: DesktopSettings(hide_on_screenshot=False), driver_factory=lambda: native)
        desktop.start()
        try:
            result = await desktop.perform_actions((ScreenAction(0.5, 0.5, "click", wait_seconds=0),))
            assert result.status == "completed" and result.screenshot is None
            assert result.error == "截图失败"
            assert len(native.actions) == 1
        finally:
            await desktop.close()
    asyncio.run(scenario())


def test_registration_rejects_other_process_and_stale_revision(tmp_path) -> None:
    """身份变化通过公开登记接口验证，不让旧快照扩大隐藏范围。

    Args:
        tmp_path (Path): Pytest 为本用例创建的独立目录，不访问生产存储。
    """
    async def scenario():
        """登记一次后提交过期快照与不属于当前进程的句柄。"""
        import pytest
        native = FakeDesktop(tmp_path / "restore.json", owner_pid=100)
        native.windows[13]["owner"] = 200
        desktop = DesktopController(lambda: DesktopSettings(), driver_factory=lambda: native)
        desktop.start()
        try:
            await desktop.register_windows(DesktopWindowRegistration(owner_pid=100, revision=5, windows={"pet": "11", "chat": "12"}))
            receipt = await desktop.register_windows(DesktopWindowRegistration(owner_pid=100, revision=4, windows={"pet": "13"}))
            assert not receipt["accepted"]
            with pytest.raises(ValueError):
                await desktop.register_windows(DesktopWindowRegistration(owner_pid=100, revision=6, windows={"pet": "13"}))
            await desktop.capture_screen()
            assert native.observed == [[13]]
        finally:
            await desktop.close()
    asyncio.run(scenario())


def test_cancelling_wait_does_not_release_desktop_before_thread_cleanup(tmp_path) -> None:
    """异步取消后仍等工作线程退出，下一截图不能进入未清理的桌面。

    Args:
        tmp_path (Path): Pytest 为本用例创建的独立目录，不访问生产存储。
    """
    async def scenario():
        """阻塞模拟截图，在取消和第二次请求之间观察串行清理。"""
        entered, release = threading.Event(), threading.Event()

        class BlockingDesktop(FakeDesktop):
            """用可控操作系统等待模拟无法立即取消的捕获。"""
            def capture(self):
                """首次捕获暂停，测试释放后再返回观察。"""
                if not self.observed:
                    entered.set()
                    assert release.wait(2)
                return super().capture()

        native = BlockingDesktop(tmp_path / "restore.json")
        desktop = DesktopController(lambda: DesktopSettings(hide_on_screenshot=False), driver_factory=lambda: native)
        desktop.start()
        first = asyncio.create_task(desktop.capture_screen())
        while not entered.is_set():
            await asyncio.sleep(0.005)
        first.cancel()
        second = asyncio.create_task(desktop.capture_screen())
        await asyncio.sleep(0.05)
        assert not first.done() and not second.done()
        release.set()
        await asyncio.gather(first, return_exceptions=True)
        await second
        assert len(native.observed) == 2
        await desktop.close()
    asyncio.run(scenario())


def test_failed_restore_record_is_not_overwritten_by_next_capture(tmp_path) -> None:
    """恢复失败留下的精确窗口记录必须保留，后续操作先重试恢复再重新隐藏。

    Args:
        tmp_path (Path): Pytest 为本用例创建的独立目录，不访问生产存储。
    """
    import json
    import pytest

    class FailingRestore(FakeDesktop):
        """模拟桌宠暂时不能恢复，聊天仍可正常恢复。"""
        fail = True

        def show_inactive(self, hwnd):
            """仅桌宠恢复失败，不阻止控制器尝试其他角色。

            Args:
                hwnd (int): 测试操作系统或 Electron 登记的精确原生窗口句柄。
            """
            if hwnd == 11 and self.fail:
                raise OSError("桌宠恢复失败")
            super().show_inactive(hwnd)

    async def scenario():
        """连续失败不丢记录，系统恢复正常后原窗口都能清理。"""
        native = FailingRestore(tmp_path / "restore.json")
        restore = tmp_path / "restore.json"
        desktop = DesktopController(lambda: DesktopSettings(), driver_factory=lambda: native)
        desktop.start()
        try:
            await desktop.register_windows(DesktopWindowRegistration(owner_pid=100, revision=1, windows={"pet": "11", "chat": "12"}))
            with pytest.raises(RuntimeError):
                await desktop.capture_screen()
            record = json.loads(restore.read_text())
            with pytest.raises(RuntimeError):
                await desktop.capture_screen()
            assert json.loads(restore.read_text()) == record
            assert len(native.observed) == 1
            assert not native.windows[11]["visible"] and native.windows[12]["visible"]
            native.fail = False
            await desktop.capture_screen()
            assert native.windows[11]["visible"] and not restore.exists()
        finally:
            await desktop.close()
    asyncio.run(scenario())


def test_new_electron_can_discard_restore_record_of_destroyed_old_windows(tmp_path) -> None:
    """整个应用崩溃后旧 HWND 已销毁，新主进程不能被旧 PID 的记录永久阻塞。

    Args:
        tmp_path (Path): Pytest 为本用例创建的独立目录，不访问生产存储。
    """
    import json

    async def scenario():
        """新登记只包含新窗口，确认清理旧记录后正常捕获和恢复。"""
        restore = tmp_path / "restore.json"
        restore.write_text(json.dumps({"owner_pid": 90, "backend_pid": 200,
                                       "windows": [{"role": "pet", "hwnd": "91"}]}))
        native = FakeDesktop(restore, owner_pid=100)
        desktop = DesktopController(lambda: DesktopSettings(), driver_factory=lambda: native)
        desktop.start()
        try:
            await desktop.register_windows(DesktopWindowRegistration(owner_pid=100, revision=1, windows={"pet": "11", "chat": "12"}))
            await desktop.capture_screen()
            assert native.observed == [[13]] and not restore.exists()
        finally:
            await desktop.close()
    asyncio.run(scenario())
