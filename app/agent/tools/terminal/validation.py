"""终端工具共享的危险输入检查和错误观察表达。"""

from app.agent.tools.core.base import ToolResult


# 既有 PowerShell 硬拦截继续适用于初始命令和后续输入；它不是系统沙箱。
DANGEROUS_PATTERNS = (
    "remove-item", "rd /s /q", "rmdir /s /q", "del /f /s /q", "erase /f /s /q", "cipher /w",
    "format-volume", "clear-disk", "diskpart", "remove-partition", "delete partition", "delete volume",
    "stop-computer", "restart-computer", "shutdown", "bootrec", "bcdedit",
    "vssadmin delete shadows", "wmic shadowcopy delete", "wbadmin delete",
    "reg delete", "remove-itemproperty", "set-itemproperty -path hk", "new-itemproperty -path hk",
    "netsh advfirewall set allprofiles state off", "set-mppreference -disablerealtimemonitoring",
    "net user ", "net localgroup administrators", "add-localgroupmember",
)


def validate_input(text: str) -> None:
    """在启动或写入前保留既有危险内容硬拦截。

    Args:
        text (str): 初始命令或准备发送的 stdin。

    Raises:
        ValueError: 命中了既有危险命令规则，输入尚未发送。
    """
    if any(pattern in text.lower() for pattern in DANGEROUS_PATTERNS):
        raise ValueError("检测到潜在危险命令，已阻止执行；输入未发送。")


def failure(message: str) -> ToolResult:
    """直接返回调用失败的原因，不附加输入状态或固定操作提示。

    Args:
        message (str): 校验或运行器提供的失败原因。

    Returns:
        ToolResult: 模型可读的简短错误说明。
    """
    return ToolResult.success(f"错误：{message}")
