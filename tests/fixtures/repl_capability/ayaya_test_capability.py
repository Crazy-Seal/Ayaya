"""为原生 REPL 行为测试提供独立于应用包的轻量能力。"""

import io
import sys
import threading
from pathlib import Path

from repl_api import JsonValue, ReplHost


class TestCapability:
    """通过公开 host 验证配置、观察输出、中断和执行清理。"""

    def __init__(self, config: dict[str, JsonValue], host: ReplHost) -> None:
        """保存能力配置和 runner 提供的接口。

        Args:
            config (dict[str, JsonValue]): 准备阶段传入的初始化参数。
            host (ReplHost): 当前能力可以使用的 REPL 公共接口。
        """
        self.config = config
        self.host = host
        self.old_id = ""

    def inspect(self) -> None:
        """显示配置并确认能力不依赖后端 app 模块。"""
        print(self.config["value"], "app" in sys.modules)

    def observe(self) -> None:
        """内部编码并显式输出一张测试图片。"""
        from PIL import Image
        data = io.BytesIO()
        Image.new("RGB", (2, 3), "red").save(data, format="PNG")
        self.host.emit_image(data.getvalue())

    def cleanup(self, label: str, fail: bool = False) -> None:
        """登记带文字结果的清理，供测试验证逆序和故障后继续。

        Args:
            label (str): 清理时输出的标识。
            fail (bool): 是否模拟清理异常。
        """
        def action() -> None:
            """输出本次清理标识并按用例模拟故障。"""
            print(label)
            if fail:
                raise ValueError("测试清理失败")
        self.host.defer_cleanup(self.host.execution_id, action)

    def interrupt_later(self, delay: float) -> None:
        """模拟绑定当前执行编号的异步 ESC 回调。

        Args:
            delay (float): 触发前等待的秒数。
        """
        execution_id = self.host.execution_id
        self.old_id = execution_id
        timer = threading.Timer(delay, self.host.request_interrupt, args=(execution_id,))
        self.host.defer_cleanup(execution_id, timer.cancel)
        timer.start()

    def cleanup_file(self, path: str) -> None:
        """在本次资源释放时创建标记，供 Agent 停止测试核验顺序。

        Args:
            path (str): 当前工作目录内的测试标记。
        """
        self.host.defer_cleanup(self.host.execution_id, lambda: Path(path).touch())

    def interrupt_old(self) -> None:
        """模拟旧执行的迟到中断，当前执行应继续完成。"""
        self.host.request_interrupt(self.old_id)

    def interrupt_next_compile(self) -> None:
        """在下一次编译即将完成时请求中断，验证执行入口不会漏掉请求。"""
        import builtins
        import ctypes
        original = builtins.compile

        def compile_once(*args, **kwargs):
            """编译后恢复原函数，并在尚未进入模型帧时交付信号。

            Args:
                *args (tuple): 原生 compile 的位置参数。
                **kwargs (dict): 原生 compile 的关键字参数。

            Returns:
                object: 本次编译出的代码对象。
            """
            builtins.compile = original
            compiled = original(*args, **kwargs)
            self.host.request_interrupt(self.host.execution_id)
            ctypes.windll.kernel32.Sleep(50)
            return compiled

        builtins.compile = compile_once

    def notice(self, active: bool = True, text: str = "测试控制中") -> None:
        """发布当前执行的测试提示。

        Args:
            active (bool): 开启或关闭提示。
            text (str): 开启或更新时的文字。
        """
        payload: dict[str, JsonValue] = {"id": self.host.execution_id + ":control", "active": active, "text": text,
            "plugin_name": "伪造插件", "session_id": "other", "run_id": "other", "task_id": "other"}
        self.host.publish_event(self.host.execution_id, "status_notice", payload)
        payload["text"] = "发送后修改"


def create(config: dict[str, JsonValue], host: ReplHost) -> TestCapability:
    """按 runner 的固定入口创建能力对象。

    Args:
        config (dict[str, JsonValue]): 初始化参数。
        host (ReplHost): 输出、事件与执行控制接口。

    Returns:
        TestCapability: 模型可直接调用的测试对象。
    """
    if config.get("fail"):
        raise ValueError("测试初始化失败")
    if config.get("initialization_marker"):
        import time
        Path(str(config["initialization_marker"])).touch()
        time.sleep(60)
    return TestCapability(config, host)
