"""定义插件能力与原生 REPL 之间的标准库公共契约。"""

from dataclasses import dataclass, field
from pathlib import Path
from enum import StrEnum
from collections.abc import Callable
from typing import Protocol, TypeAlias

JsonValue: TypeAlias = str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]


class ReplEndReason(StrEnum):
    """统一父子进程报告的代码完成、错误、中断及进程故障原因。"""

    COMPLETED = "completed"
    ERROR = "error"
    INTERRUPTED = "interrupted"
    TIMEOUT = "timeout"
    CRASHED = "crashed"


@dataclass(frozen=True)
class ReplCapability:
    """声明插件需要预先放入 Python 命名空间的能力。

    Attributes:
        module_root (Path): 插件能力模块所在目录。
        module_name (str): runner 导入的模块名。
        export_name (str): 模型代码直接使用的变量名。
        config (dict[str, JsonValue]): 交给能力工厂的初始化参数。
    """

    module_root: Path
    module_name: str
    export_name: str
    config: dict[str, JsonValue] = field(default_factory=dict)


class ReplHost(Protocol):
    """为插件能力提供图片、事件、中断和当前执行清理接口。"""

    @property
    def execution_id(self) -> str:
        """读取当前代码调用编号。

        Returns:
            str: 供异步回调捕获的执行编号。
        """
        ...

    def emit_image(self, image_bytes: bytes) -> None:
        """向本次工具结果输出编码图片。

        Args:
            image_bytes (bytes): PNG 或 JPEG 文件编码。
        """
        ...

    def publish_event(self, execution_id: str, name: str, payload: dict[str, JsonValue]) -> None:
        """发布绑定本次执行的插件事件。

        Args:
            execution_id (str): 回调所属执行编号。
            name (str): 框架支持的插件事件名称。
            payload (dict[str, JsonValue]): 可序列化的展示字段。
        """
        ...

    def request_interrupt(self, execution_id: str) -> None:
        """请求停止匹配编号的当前代码。

        Args:
            execution_id (str): 要停止的执行编号。
        """
        ...

    def raise_if_interrupted(self, execution_id: str) -> None:
        """在能力方法边界交付已受理中断。

        Args:
            execution_id (str): 需要检查的当前调用编号。

        Raises:
            KeyboardInterrupt: 当前执行已被用户终止。
        """
        ...

    def defer_cleanup(self, execution_id: str, callback: Callable[[], object]) -> None:
        """登记本次执行结束前必须尝试的清理。

        Args:
            execution_id (str): 资源所属的执行编号。
            callback (Callable[[], object]): 解除监听或释放输入等同步清理。
        """
        ...
