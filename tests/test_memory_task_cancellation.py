"""验证记忆具体任务直接执行业务时的取消行为。"""

import asyncio

from app.agent.plugins.runtime import PluginRuntime
from app.agent.plugins.memory.tasks.memory_tasks import DiaryTask
from app.agent.runtime.tasks.task_supervisor import TaskSupervisor
from app.schemas.runtime_types import TaskLifetime, TaskStatus


def test_diary_async_work_is_cancelled_without_waiting_for_unrelated_completion(tmp_path):
    """日记异步业务收到停止立即响应，Task 不再额外调度整段不可取消业务。

    Args:
        tmp_path (Path): 隔离任务存储。
    """
    async def scenario():
        """通过正式提交、停止和等待观察异步业务的取消。"""
        supervisor = TaskSupervisor(runtime_queue=asyncio.Queue(), db_path=str(tmp_path / 'state.db'))
        runtime = PluginRuntime('memory', 's', 'run', _supervisor=supervisor, _runtime_queue=supervisor.runtime_queue)
        started, cancelled = asyncio.Event(), asyncio.Event()

        class Memory:
            """代表尚未返回的异步日记生成调用。"""

            async def generate_diary(self, *, max_history_id):
                """等待期间响应取消。

                Args:
                    max_history_id (int): 已固定的历史截止位置。
                """
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

        try:
            task_id = await runtime.submit_task(DiaryTask(Memory(), 1), name='diary', title='日记', lifetime=TaskLifetime.INDEPENDENT, notify_agent=False)
            await started.wait()
            await supervisor.stop_task(task_id)
            assert (await runtime.wait_task(task_id, timeout_ms=1000)).status == TaskStatus.STOPPED
            assert cancelled.is_set() and supervisor.active_count() == 0
        finally:
            await supervisor.close()
    asyncio.run(scenario())
