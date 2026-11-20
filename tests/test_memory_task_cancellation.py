from tests.memory_support import operation_for
"""验证记忆具体任务直接执行业务时的取消行为。"""
from app.crud.sqlite_factory import SqliteFactory

from app.crud.task_record_dao import TaskRecordDao
from app.services.settings_mutation import SettingsMutationCoordinator

import asyncio
from datetime import date
from tests.runtime_support import configured_settings

from app.agent.plugins.memory.memories.diary import DiaryGeneration
from app.agent.plugins.runtime import PluginRuntime
from app.agent.plugins.memory.tasks.memory_tasks import DiaryTask
from app.agent.runtime.tasks.task_supervisor import TaskSupervisor
from app.schemas.runtime_types import TaskStatus


def test_diary_async_work_is_cancelled_without_waiting_for_unrelated_completion(tmp_path):
    """日记异步业务收到停止立即响应，Task 不再额外调度整段不可取消业务。

    Args:
        tmp_path (Path): 隔离任务存储。
    """
    async def scenario():
        """通过正式提交、停止和等待观察异步业务的取消。"""
        supervisor = TaskSupervisor(runtime_queue=asyncio.Queue(), records=TaskRecordDao(SqliteFactory(str(tmp_path / 'state.db'))))
        runtime = PluginRuntime('memory', 's', 'run', _supervisor=supervisor, _runtime_queue=supervisor.runtime_queue)
        started, cancelled = asyncio.Event(), asyncio.Event()

        class Memory:
            """代表尚未返回的异步日记生成调用。"""

            async def generate_diary(self, generation: DiaryGeneration) -> str:
                """等待期间响应取消。

                Args:
                    generation (DiaryGeneration): 已固定的日记日期与历史截止位置。
                """
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

        try:
            generation = DiaryGeneration("s", date(2026, 10, 25), [], 1)
            task_id = await runtime.submit_task(DiaryTask(operation_for(Memory()), generation), name='diary', title='日记', notify_agent=False)
            await started.wait()
            await supervisor.stop_task(task_id)
            assert (await runtime.wait_task(task_id, timeout_ms=1000)).status == TaskStatus.STOPPED
            assert cancelled.is_set() and supervisor.active_task_count() == 0
        finally:
            await supervisor.close()
            await supervisor.records.close()
    asyncio.run(scenario())
