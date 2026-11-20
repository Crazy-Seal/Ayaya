"""保存记忆插件随会话恢复的真人输入计数。"""

from pydantic import BaseModel, ConfigDict, Field
from app.agent.state.model import AgentState

MEMORY_STATE_KEY = "memory"


class MemoryState(BaseModel):
    """记录尚未交给长期记忆提取的真人输入数量。"""

    model_config = ConfigDict(extra="allow")
    long_term_memory_turn_counter: int = Field(default=0, ge=0, strict=True)

    @classmethod
    def load(cls, state: AgentState) -> "MemoryState":
        """读取当前会话的记忆命名空间，首次启用从零开始。

        Args:
            state (AgentState): 新激活加载的会话状态。

        Returns:
            MemoryState: 与原始状态独立的记忆计数。
        """
        return cls.model_validate(state.get_extra(MEMORY_STATE_KEY, {}))

    def save(self, state: AgentState) -> None:
        """把本次计数写回会话，随后由输入提交事务持久保存。

        Args:
            state (AgentState): 将要随输入保存的会话状态。
        """
        state.set_extra(MEMORY_STATE_KEY, self.model_dump())
