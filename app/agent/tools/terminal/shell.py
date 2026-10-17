"""探测当前主机 Shell，并为工具描述和实际启动提供同一份配置。"""

from dataclasses import dataclass
from functools import lru_cache
import os
import shutil
import subprocess
import sys


@dataclass(frozen=True)
class ShellEnvironment:
    """保存已经探测成功的 Shell 信息。

    Attributes:
        kind (str): Shell 类型，决定命令语法和启动参数。
        executable (str): 实际执行程序的绝对路径。
        version (str): 探测到的版本说明。
    """

    kind: str
    executable: str
    version: str

    def command(self, text: str) -> list[str]:
        """构造执行命令的参数，不额外保留已经结束的 Shell。

        Args:
            text (str): 使用当前 Shell 语法编写的原始命令。

        Returns:
            list[str]: 可直接传给进程 Adapter 的程序和参数。
        """
        if self.kind in {"pwsh", "powershell"}:
            prefix = (
                "$utf8 = New-Object System.Text.UTF8Encoding($false);"
                "[Console]::InputEncoding = $utf8;"
                "[Console]::OutputEncoding = $utf8;$OutputEncoding = $utf8;"
            )
            return [self.executable, "-NoLogo", "-NoProfile", "-Command", prefix + text]
        if self.kind == "cmd":
            return [self.executable, "/d", "/s", "/c", "chcp 65001 >nul & " + text]
        return [self.executable, "-c", text]

    def describe(self) -> str:
        """向模型说明实际 Shell 及跨调用状态规则。

        Returns:
            str: 与执行配置一致的模型可见说明。
        """
        return (
            f"当前 Shell：{self.kind}；程序：{self.executable}；版本：{self.version}。"
            "命令必须使用该 Shell 的语法。每次 exec_command 创建新进程；"
            "需要保留 Shell、SSH 或程序状态时，启动交互程序并用 task_id 调用 write_stdin。"
            "tty=true 提供终端回显、控制字符等行为，不保证进程在命令结束后保留。"
        )


@lru_cache(maxsize=1)
def detect_shell() -> ShellEnvironment:
    """按操作系统探测并缓存首个可以执行的 Shell，不读取生产配置。

    Returns:
        ShellEnvironment: 工具描述和命令执行共享的 Shell 快照。

    Raises:
        RuntimeError: 所有候选 Shell 均不存在或无法正常启动。
    """
    candidates = (
        ("pwsh", "powershell", "cmd") if os.name == "nt" else
        ("zsh", "bash", "sh") if sys.platform == "darwin" else ("bash", "sh")
    )
    # 1. 每个候选既检查路径也实际运行，避免仅发现损坏的可执行文件。
    for kind in candidates:
        executable = shutil.which(kind)
        if not executable:
            continue
        if kind in {"pwsh", "powershell"}:
            args = ["-NoLogo", "-NoProfile", "-Command", "$PSVersionTable.PSVersion.ToString()"]
        elif kind == "cmd":
            args = ["/d", "/c", "ver"]
        elif kind == "sh":
            args = ["-c", "printf '%s' 'POSIX sh（未提供独立版本号）'"]
        else:
            args = ["--version"]
        try:
            result = subprocess.run(
                [executable, *args], capture_output=True, timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            if result.returncode == 0:
                version = result.stdout.decode("utf-8", errors="replace").strip().splitlines()
                return ShellEnvironment(kind, os.path.abspath(executable), version[0][:256] if version else "未知")
        except (OSError, subprocess.TimeoutExpired):
            continue
    raise RuntimeError("未找到可运行的默认 Shell，请安装本操作系统支持的 Shell。")
