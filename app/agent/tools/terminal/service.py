"""提供系统终端创建能力，平台终端对象负责实际读写及资源释放。"""

from app.agent.tools.terminal.process import TerminalOperations, spawn_terminal


class TerminalService:
    """按系统和 tty 创建终端，不保存任务、会话或任务管理对象。"""

    async def spawn(self, argv: list[str], cwd: str, tty: bool) -> TerminalOperations:
        """创建具有统一读写、等待及释放方法的系统终端。

        Args:
            argv (list[str]): 实际启动的程序及参数。
            cwd (str): 已校验的工作目录。
            tty (bool): 是否使用真实终端。

        Returns:
            TerminalOperations: 持有实际进程及平台资源的终端对象。
        """
        return await spawn_terminal(argv, cwd, tty)
