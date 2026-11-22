from app.agent.state.manager import SessionStateManager
"""通过工具运行时和真实 Supervisor 验证任务对象及会话归属。"""
from tests.runtime_support import collect_agent_events
from tests.runtime_support import runtime_hub, close_test_runtime
from app.routes.agent import observe_runtime
from tests.runtime_support import build_test_scheduler
from app.crud.sqlite_factory import SqliteFactory

from app.crud.task_record_dao import TaskRecordDao
from app.services.settings_mutation import SettingsMutationCoordinator

import asyncio

from app.agent.runtime.tasks.base import BaseTask
from tests.runtime_support import configured_settings
from app.agent.tools.core.runtime import ToolRuntime
from app.agent.runtime.tasks.task_supervisor import TaskSupervisor


def test_tools_share_session_task_but_other_sessions_cannot_access(tmp_path):
    """写入工具可取得命令工具的原 Task，其他会话不能读取对象或记录。

    Args:
        tmp_path (Path): 隔离数据库目录。
    """
    async def scenario():
        """提交一个有明确结束时机的任务，观察运行和完成后的公开查询。"""
        queue = asyncio.Queue()
        supervisor = TaskSupervisor(runtime_queue=queue, records=TaskRecordDao(SqliteFactory(str(tmp_path / 'state.db'))))
        release = asyncio.Event()

        class Job(BaseTask):
            """等待外部事件的具体任务。"""

            async def run(self, runtime):
                """等待测试结束事件。

                Args:
                    runtime (TaskRuntime): 本次任务的固定来源。
                """
                await release.wait()

        owner = ToolRuntime('s', 'run', 'exec_command', 'call', _supervisor=supervisor, _runtime_queue=queue)
        writer = ToolRuntime('s', 'next', 'write_stdin', 'write', _supervisor=supervisor, _runtime_queue=queue)
        outsider = ToolRuntime('other', 'run', 'write_stdin', 'write', _supervisor=supervisor, _runtime_queue=queue)
        job = Job()
        try:
            task_id = await owner.submit_task(job, name='test', title='测试', notify_agent=False)
            assert supervisor.get_task(task_id) is job
            assert await writer.get_task(task_id) is job
            assert await outsider.get_task(task_id) is None
            assert await outsider.get_task_snapshot(task_id) is None
            assert await writer.get_task('missing') is None
            record = await writer.get_task_snapshot(task_id)
            assert record.tool_name == 'exec_command' and record.origin_tool_call_id == 'call'
            assert record.origin_run_id == 'run'
            release.set()
            await writer.wait_task(task_id, timeout_ms=1000)
            assert supervisor.get_task(task_id) is None
            assert await writer.get_task_snapshot(task_id) is not None
        finally:
            release.set()
            await supervisor.close()
            await supervisor.records.close()
    asyncio.run(scenario())


def test_terminal_can_finish_between_record_and_object_lookup(tmp_path, monkeypatch):
    """查询运行对象期间终端刚结束时，写入工具返回最新终态。

    Args:
        tmp_path (Path): 隔离进程文件及任务数据库。
        monkeypatch (pytest.MonkeyPatch): 在公开查询入口安排进程完成。
    """
    import sys
    from app.agent.state.model import AgentState
    from app.agent.tools.core.base import ToolContext
    from app.agent.tools.terminal.task import TerminalTask
    from app.agent.tools.terminal.service import TerminalService
    from app.agent.tools.write_stdin import WriteStdinTool

    async def scenario():
        """真实进程等待文件，再在工具查询对象前完成并提交终态。"""
        supervisor = TaskSupervisor(runtime_queue=asyncio.Queue(), records=TaskRecordDao(SqliteFactory(str(tmp_path / 'state.db'))))
        runtime = ToolRuntime('s', 'run', 'exec_command', 'call', _supervisor=supervisor, _runtime_queue=supervisor.runtime_queue)
        gate = tmp_path / 'release'
        script = 'import os,time\nwhile not os.path.exists(' + repr(str(gate)) + '): time.sleep(0.01)'
        task = TerminalTask('wait file', [sys.executable, '-c', script], str(tmp_path), False,
            tmp_path / 'logs', runtime.records, TerminalService())
        original_get = ToolRuntime.get_task

        async def finish_before_lookup(caller, task_id):
            """保持原查询行为，在任务对象读取前先完成真实进程。

            Args:
                caller (ToolRuntime): 发起查询的工具运行时。
                task_id (str): 查询编号。

            Returns:
                BaseTask | None: 完成清理后已经释放的对象为空。
            """
            gate.touch()
            await supervisor.wait(task_id, timeout_ms=2000)
            return await original_get(caller, task_id)

        try:
            task_id = await runtime.submit_task(task, name='terminal', title='等待', notify_agent=False)
            await task.ready.wait()
            monkeypatch.setattr(ToolRuntime, 'get_task', finish_before_lookup)
            context = ToolContext('s', AgentState(session_id='s'), runtime=runtime)
            result = await WriteStdinTool().execute({'task_id': task_id, 'yield_time_ms': 0}, context)
            assert '任务已完成' in result.text
            assert '错误' not in result.text
        finally:
            gate.touch()
            await supervisor.close()
            await supervisor.records.close()
    asyncio.run(scenario())


def test_stop_request_is_one_way_and_checked_by_scheduler(tmp_path):
    """工具停止请求入队即返回，消费者拒绝跨会话请求并接受同会话请求。

    Args:
        tmp_path (Path): 隔离任务及会话数据库。
    """
    from app.agent.runtime.agent_runtime_scheduler import AgentRuntimeScheduler
    from app.schemas.runtime_types import TaskStatus

    async def scenario():
        """真实消费请求队列，使用持久状态观察停止结果。"""
        queue = asyncio.Queue()
        supervisor_settings = configured_settings()
        supervisor_mutations = SettingsMutationCoordinator()
        supervisor = TaskSupervisor(runtime_queue=queue, records=TaskRecordDao(SqliteFactory(str(tmp_path / 'state.db'))))
        scheduler = build_test_scheduler(runtime_queue=queue, task_supervisor=supervisor,
            agent_factory=lambda session_id, manager: None, chat_settings_dao=supervisor_settings, mutation_coordinator=supervisor_mutations, session_state_manager_factory=lambda context: SessionStateManager(context, SqliteFactory()))
        owner = ToolRuntime('s', 'run', 'exec_command', 'call', _supervisor=supervisor, _runtime_queue=queue)
        outsider = ToolRuntime('other', 'run', 'write_stdin', 'call', _supervisor=supervisor, _runtime_queue=queue)
        started = asyncio.Event()

        class Job(BaseTask):
            """在收到停止前持续等待。"""

            async def run(self, runtime):
                """等待取消来结束当前业务。

                Args:
                    runtime (TaskRuntime): 本次任务来源。
                """
                started.set()
                await asyncio.Event().wait()

        await scheduler.start()
        try:
            task_id = await owner.submit_task(Job(), name='test', title='等待', notify_agent=False)
            await started.wait()
            assert await outsider.stop_task(task_id) is None
            await asyncio.wait_for(queue.join(), 1)
            assert (await owner.get_task_snapshot(task_id)).status == TaskStatus.RUNNING
            assert await owner.stop_task(task_id) is None
            await asyncio.wait_for(queue.join(), 1)
            assert (await owner.wait_task(task_id, timeout_ms=1000)).status == TaskStatus.STOPPED
        finally:
            await close_test_runtime(scheduler)
            await supervisor.close()
            await supervisor.records.close()
    asyncio.run(scenario())


def test_agent_injects_tool_runtime_and_runs_background_terminal(tmp_path, monkeypatch):
    """实际 Agent 装配工具，模型后台调用保留工具来源并继续收到工具结果。

    Args:
        tmp_path (Path): 隔离数据目录。
        monkeypatch (pytest.MonkeyPatch): 将命令目录限制在临时目录。
    """
    from app.agent.agent import Agent, AgentConfig
    from app.agent.context import ContextStrategyConfig
    from app.agent.models.llm_client import StreamChunk
    from app.agent.state.messages import ToolCall, ToolMessage
    monkeypatch.setattr('app.agent.tools.exec_command.WORKDIR', tmp_path)

    async def scenario():
        """只替代模型，真实执行终端和任务存储。"""
        supervisor = TaskSupervisor(runtime_queue=asyncio.Queue(), records=TaskRecordDao(SqliteFactory(str(tmp_path / 'state.db'))))
        calls = []

        class Model:
            """先要求后台命令，随后检查真实工具消息。"""

            async def astream(self, messages, tools=None):
                """依次发出工具调用和最终回答。

                Args:
                    messages (Sequence): 框架组装的模型输入。
                    tools (Sequence | None): 工具定义。

                Yields:
                    StreamChunk: 一次工具请求或结束回答。
                """
                calls.append(messages)
                if len(calls) == 1:
                    yield StreamChunk(tool_call=ToolCall('command-call', 'exec_command', {'cmd': 'echo TOOL_RUNTIME_OK', 'yield_time_ms': 0}))
                else:
                    assert any(isinstance(message, ToolMessage) and 'task_id=' in message.text for message in messages)
                    yield StreamChunk(content='已提交')

            async def close(self):
                """模型替身没有需要关闭的网络资源。"""

        agent = Agent(AgentConfig(session_id='s', model_name='test', api_key='test', tools=['exec_command'], plugins=[],
            context_strategy=ContextStrategyConfig()), sqlite_factory=SqliteFactory(str(tmp_path / 'state.db')), llm_client=Model(),
            tool_runtime_factory=lambda tool_name, run_id, tool_call_id: ToolRuntime(
                's', run_id, tool_name, tool_call_id, _supervisor=supervisor, _runtime_queue=supervisor.runtime_queue))
        try:
            async def receive_agent_event(_):
                """在原事件位置执行此场景的观察和控制。"""
                pass
            await agent.run('执行命令', emit=receive_agent_event)
            records = await supervisor.list_tasks('s')
            assert len(records) == 1
            assert records[0].tool_name == 'exec_command'
            assert records[0].origin_tool_call_id == 'command-call'
            await supervisor.wait(records[0].task_id, timeout_ms=2000)
        finally:
            await supervisor.close()
            await supervisor.records.close()
            await agent.close()
    asyncio.run(scenario())
