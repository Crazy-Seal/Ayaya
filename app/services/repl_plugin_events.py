"""把 REPL 能力事件接入已有插件通路，并关闭本次执行的提示。"""

import asyncio
import logging

from app.agent.plugins.runtime import PluginRuntime
from app.agent.runtime.requests import RuntimeRequest
from app.agent.runtime.tasks.task_supervisor import TaskSupervisor
from app.repl.contracts import ReplEventHandler, ReplExecutionFinished, ReplPluginEvent
from app.schemas.runtime_types import InteractionEventKind

logger = logging.getLogger(__name__)


def create_repl_plugin_event_handler(supervisor: TaskSupervisor,
                                     runtime_queue: asyncio.Queue[RuntimeRequest]) -> ReplEventHandler:
    """为应用唯一 REPL 创建事件接入函数，记录仅用于当前执行收尾。

    Args:
        supervisor (TaskSupervisor): PluginRuntime 借用的应用任务入口。
        runtime_queue (asyncio.Queue[RuntimeRequest]): 现有 Scheduler 独占消费的请求队列。

    Returns:
        ReplEventHandler: 顺序受理能力事件和本次执行结束通知的函数。
    """
    execution_id = ""
    opened: dict[tuple[str, str], PluginRuntime] = {}

    async def handle(event: ReplPluginEvent | ReplExecutionFinished) -> None:
        """绑定来源发布事件，并在返回前把本次提示关闭入队。

        Args:
            event (ReplPluginEvent | ReplExecutionFinished): REPL 核心交付的能力事件或结束通知。
        """
        nonlocal execution_id
        if isinstance(event, ReplExecutionFinished):
            if event.execution_id != execution_id:
                return
            try:
                for (_, notice_id), runtime in tuple(opened.items()):
                    try:
                        await runtime.publish_event(InteractionEventKind.STATUS_NOTICE,
                                                    {"id": notice_id, "active": False})
                    except Exception:
                        logger.exception("关闭 REPL 插件持续提示失败")
            finally:
                opened.clear()
                execution_id = ""
            return
        execution_id = event.execution_id
        runtime = PluginRuntime(event.capability.plugin_name, event.context.session_id,
                                _supervisor=supervisor, _runtime_queue=runtime_queue)
        await runtime.publish_event(event.name, event.payload)
        # 1. 载荷受理由 ParentSession 负责；此处只记下可用于关闭提示的编号。
        notice_id = event.payload.get("id")
        if event.name == InteractionEventKind.STATUS_NOTICE and isinstance(notice_id, str):
            key = (event.capability.plugin_name, notice_id)
            if event.payload.get("active") is True:
                opened[key] = runtime
            elif event.payload.get("active") is False:
                opened.pop(key, None)

    return handle
