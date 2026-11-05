"""为真实进程测试组装任务运行时，直接使用公共任务方法观察执行。"""

from pathlib import Path

from app.agent.tools.core.runtime import ToolRuntime
from app.agent.tools.terminal.service import TerminalService
from app.agent.tools.terminal.task import TerminalTask, read_terminal_output, read_terminal_tail
from app.agent.tools.terminal.models import TaskLaunchResult
from app.environment import get_data_dir
from app.schemas.runtime_types import TaskStatus
from app.schemas.task_types import TERMINAL_TASK_NAME


class TerminalHarness:
    """以直接 argv 保留跨平台进程测试，不经过模型或 Shell 参数转换。

    Attributes:
        supervisor (TaskSupervisor): 使用临时数据库的真实管理对象。
        spool_dir (Path): 当前测试输出目录。
    """

    def __init__(self, supervisor, *, spool_dir=None):
        """保存测试所用的真实共享对象。

        Args:
            supervisor (TaskSupervisor): 通用任务管理对象。
            spool_dir (Path | None): 输出目录，省略时使用测试数据目录。
        """
        self.supervisor = supervisor
        self.spool_dir = Path(spool_dir or get_data_dir() / 'tasks')

    def runtime(self, session_id='chat', run_id='', tool_name='exec_command', tool_call_id=''):
        """创建指定来源的真实工具运行时。

        Args:
            session_id (str): 测试会话。
            run_id (str): 来源轮次。
            tool_name (str): 来源工具。
            tool_call_id (str): 模型调用编号。

        Returns:
            ToolRuntime: 与当前 Supervisor 和队列相连的运行时。
        """
        return ToolRuntime(session_id, run_id, tool_name, tool_call_id,
            _supervisor=self.supervisor, _runtime_queue=self.supervisor.runtime_queue)

    async def launch_terminal(self, session_id, command, argv, *, cwd, tty=False,
                              yield_time_ms=10000, tool_name='exec_command',
                              origin_run_id=None, origin_tool_call_id=None):
        """提交真实 TerminalTask 并使用其交互方法取得初次观察。

        Args:
            session_id (str): 测试会话。
            command (str): 记录中的命令说明。
            argv (list[str]): 直接进程启动参数。
            cwd (str): 工作目录。
            tty (bool): 是否创建 PTY。
            yield_time_ms (int): 初次观察预算。
            tool_name (str): 创建任务的工具名称。
            origin_run_id (str | None): 来源轮次。
            origin_tool_call_id (str | None): 来源调用编号。

        Returns:
            TaskLaunchResult: 真实任务交互结果。
        """
        if not tool_name.strip():
            raise ValueError('新任务必须提供工具名')
        runtime = self.runtime(session_id, origin_run_id or '', tool_name, origin_tool_call_id or '')
        task = TerminalTask(command, argv, cwd, tty, self.spool_dir, runtime.records, TerminalService())
        task_id = await runtime.submit_task(task, name=TERMINAL_TASK_NAME, title=command,
            notify_agent=True, background=yield_time_ms == 0)
        result = await task.interact(runtime, yield_time_ms=yield_time_ms, initial=True)
        if result.background:
            await runtime.mark_background(task_id)
        return result

    async def interact(self, session_id, task_id, *, chars='', yield_time_ms=10000):
        """取得原任务对象，调用其公开交互方法。

        Args:
            session_id (str): 当前会话。
            task_id (str): 目标任务编号。
            chars (str): 输入内容。
            yield_time_ms (int): 观察预算。

        Returns:
            TaskLaunchResult: 当前任务状态和增量。

        Raises:
            KeyError: 编号不存在或属于其他会话。
        """
        runtime = self.runtime(session_id, tool_name='write_stdin')
        record = await runtime.get_task_record(task_id)
        if record is None:
            raise KeyError('任务不存在或不属于当前聊天')
        task = await runtime.get_task(task_id)
        if task is None:
            return TaskLaunchResult(task_id, record.status == TaskStatus.RUNNING, '', record.status,
                record.exit_code, error='任务已结束，输入未发送' if chars else None)
        return await task.interact(runtime, chars=chars, yield_time_ms=yield_time_ms)

    async def read_output(self, task_id, offset, limit):
        """通过持久记录读取输出，不需要运行中的任务对象。

        Args:
            task_id (str): 目标编号。
            offset (int): 字节位置。
            limit (int): 字节预算。

        Returns:
            dict: 输出页。
        """
        return await read_terminal_output(await self.supervisor.get_task_record(task_id), offset, limit)

    async def read_tail(self, task_id):
        """通过记录读取有限末尾。

        Args:
            task_id (str): 目标编号。

        Returns:
            str: 末尾输出。
        """
        return await read_terminal_tail(await self.supervisor.get_task_record(task_id))
