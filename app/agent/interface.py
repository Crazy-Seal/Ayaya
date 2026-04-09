from app.schemas.chat import AgentInput


class BaseAgent:
    def invoke_agent_stream(self, user_message: AgentInput) -> object:
        """流式调用入口，子类实现具体逻辑。"""
        pass

    def rollback_thread_checkpoints(self, checkpoint_ns: str = "") -> tuple[int, int]:
        """回滚本轮会话中基线之后写入的checkpoint，子类实现具体逻辑。"""
        pass
