"""应用共享请求队列的联合类型，所有请求只携带数据和专用 Future。"""

from app.schemas.task_types import TaskRecord, PluginRequest, TaskStopRequest
from app.agent.runtime.subagent_requests import SubagentRequest
from app.interaction.interaction_contract import InteractionEvent

RuntimeRequest = TaskRecord | PluginRequest | TaskStopRequest | SubagentRequest | InteractionEvent | None
