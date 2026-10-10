"""直接执行 PowerShell 命令，由任务管理器保存输出并管理前台和后台进程。"""

import os
import re
from typing import Annotated, Tuple

from app.agent.tools.decorator import tool
from app.agent.tools.base import ToolContext
from app.agent.utils.infra.safe_path import WORKDIR

RUN_PS_TIMEOUT_SEC = int(os.getenv("RUN_PS_TIMEOUT_SEC", "120"))
RUN_PS_TIMEOUT_PIP_SEC = int(os.getenv("RUN_PS_TIMEOUT_PIP_SEC", "600"))
RUN_PS_TIMEOUT_PYTHON_SEC = int(os.getenv("RUN_PS_TIMEOUT_PYTHON_SEC", "5"))
RUN_PS_TIMEOUT_GIT_SEC = int(os.getenv("RUN_PS_TIMEOUT_GIT_SEC", "180"))

DANGEROUS_PATTERNS = [
    # 文件与目录破坏
    "remove-item", "rd /s /q", "rmdir /s /q", "del /f /s /q", "erase /f /s /q", "cipher /w",
    # 磁盘/分区/卷操作
    "format-volume", "clear-disk", "diskpart", "remove-partition", "delete partition", "delete volume",
    # 系统关机/重启/引导配置
    "stop-computer", "restart-computer", "shutdown", "bootrec", "bcdedit",
    # 影子副本/备份删除
    "vssadmin delete shadows", "wmic shadowcopy delete", "wbadmin delete",
    # 注册表高风险写删
    "reg delete", "remove-itemproperty", "set-itemproperty -path hk", "new-itemproperty -path hk",
    # 防火墙/防护关闭
    "netsh advfirewall set allprofiles state off", "set-mppreference -disablerealtimemonitoring",
    # 账户与权限高风险变更
    "net user ", "net localgroup administrators", "add-localgroupmember",
]


def _build_command(command: str) -> list[str]:
    """构造非交互 PowerShell 参数，进程继承后端环境而不自动激活 Conda。

    Args:
        command (str): 用户工具调用提供的 PowerShell 命令正文。

    Returns:
        list[str]: 直接交给 TaskSupervisor 启动的程序及参数，保留 UTF-8 输出设置。
    """
    utf8_command = (
        "$utf8NoBom = New-Object System.Text.UTF8Encoding($false);"
        "[Console]::InputEncoding = $utf8NoBom;"
        "[Console]::OutputEncoding = $utf8NoBom;"
        "$OutputEncoding = $utf8NoBom;"
        "chcp 65001 > $null;"
        f"{command}"
    )
    return [
        "powershell.exe", "-NoProfile", "-NonInteractive",
        "-ExecutionPolicy", "Bypass", "-Command", utf8_command,
    ]


def _select_timeout_sec(command: str) -> Tuple[int, str]:
    """按命令类别选择前台等待时长，安装、Git 网络操作和 Python 程序使用不同预算。

    Args:
        command (str): 要在 PowerShell 执行的命令文本。

    Returns:
        Tuple[int, str]: 等待秒数及超时后解释后台任务状态的提示文本。
    """
    cmd = command.strip().lower()
    if re.search(r"(^|\s)(pip|pip3)(\s+install|\s+uninstall|\s+download)\b", cmd):
        return RUN_PS_TIMEOUT_PIP_SEC, "命令执行时间长"
    if "python -m pip" in cmd or "py -m pip" in cmd:
        return RUN_PS_TIMEOUT_PIP_SEC, "命令执行时间长"
    if re.search(r"(^|\s)conda\s+(install|update|create)\b", cmd):
        return max(RUN_PS_TIMEOUT_PIP_SEC, 900), "命令执行时间长"
    if re.search(r"(^|\s)git\s+(clone|pull|fetch|submodule)\b", cmd):
        return RUN_PS_TIMEOUT_GIT_SEC, "命令执行时间长"
    if re.search(r"(^|\s)(python|py)\s+[^-].*\.py(\s|$)", cmd):
        return RUN_PS_TIMEOUT_PYTHON_SEC, "程序运行平稳，但执行时间较长，或为持续运行的服务"
    return RUN_PS_TIMEOUT_SEC, "命令执行时间长"


async def _run_command(
    command: str, context: ToolContext | None, *, run_in_background: bool = False
) -> str:
    """保留危险命令硬拦截，将前台、后台和超时等待统一交给任务启动器。

    Args:
        command (str): 需要执行或展示的 PowerShell 命令正文。
        context (ToolContext | None): 后端注入的工具上下文。
        run_in_background (bool): 是否立即返回后台任务标识。

    Returns:
        str: 前台输出、后台 task_id 提示或已格式化的错误信息。
    """
    # 1. 危险命令先硬拦截，工具授权 allow 也不能绕过这一步。
    cmd_lower = command.lower()
    if any(d in cmd_lower for d in DANGEROUS_PATTERNS):
        return "错误: 检测到潜在危险命令，已阻止执行。"

    # 2. 直接构造 PowerShell 参数；任务管理器使用后端环境和现有工作目录启动一次进程。
    final_cmd = _build_command(command)

    timeout_sec, report = _select_timeout_sec(command)
    if context is None or context.task_launcher is None:
        return "错误: 后台任务运行器未就绪。"
    # 3. 使用共享任务端口启动一次进程，前台超时只改变等待方式。
    try:
        result = await context.task_launcher.launch(
            context.session_id, command, final_cmd,
            cwd=str(WORKDIR), background=run_in_background, timeout_sec=timeout_sec,
            origin_run_id=context.origin_run_id or None,
            origin_tool_call_id=context.origin_tool_call_id or None,
        )
        # 4. 后台返回稳定 task_id，前台完成返回有限输出；完整日志始终由任务接口读取。
        if result.background:
            if run_in_background:
                return f"后台任务已启动，task_id={result.task_id}。"
            return f"{report}，超过 {timeout_sec}s 后转入后台，task_id={result.task_id}。"
        return result.output
    except Exception as e:
        return f"错误: 命令执行失败: {e}"


@tool
async def run_ps(
    command: Annotated[str, "PowerShell 命令字符串。"],
    run_in_background: Annotated[bool, "立即在后台运行；任务结束时 Ayaya 会收到通知。"] = False,
    context: ToolContext | None = None,
) -> str:
    """运行 PowerShell 命令并返回输出结果。警告：绝对禁止运行可能对系统造成损害的命令，如删除文件、操作磁盘或注册表、重启或关闭计算机等。

    Args:
        command (str): 需要执行或展示的 PowerShell 命令正文。
        run_in_background (bool): 是否立即返回后台任务标识。
        context (ToolContext | None): 后端注入的工具上下文。

    Returns:
        str: 前台输出、后台 task_id 提示或已格式化的错误信息。
    """
    return await _run_command(command, context, run_in_background=run_in_background)
