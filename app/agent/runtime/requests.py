"""应用共享请求队列的联合类型，所有请求只携带数据和专用 Future。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING
from app.schemas.task_types import TaskRecord, PluginRequest, TaskStopRequest
from app.agent.runtime.subagent_requests import SubagentRequest
from app.interaction.interaction_contract import InteractionEvent

if TYPE_CHECKING:
    from app.agent.core.events import AgentEvent

@dataclass(frozen=True, slots=True)
class ExecutionEvent:
    """把本轮已发生的执行事件交给唯一消费者转换和发布。"""

    context_id: str
    run_id: str
    origin_channel: str
    event: AgentEvent


@dataclass(frozen=True, slots=True)
class SnapshotRequest:
    """在队列已处理的位置复制运行摘要和输出水位，数据库读取随后进行。"""

    context_id: str
    reply: asyncio.Future[tuple[dict[str, object], str, int]]


@dataclass(frozen=True, slots=True)
class ApprovalInvalidated:
    """执行故障使当前审批失效，通知展示队列移除该上下文的请求。"""

    context_id: str


@dataclass(frozen=True, slots=True)
class SessionClosed:
    """会话资源关闭完成，通知 Scheduler 移除对应实例的登记。"""

    context_id: str
    instance_id: int


RuntimeRequest = TaskRecord | PluginRequest | TaskStopRequest | SubagentRequest | InteractionEvent | ExecutionEvent | SnapshotRequest | ApprovalInvalidated | SessionClosed | None
