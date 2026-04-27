from typing import AsyncIterator, Any, cast
import logging

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langchain_core.runnables import RunnableConfig

from app.agent.checkpoint_repository import CheckpointRepository
from app.agent.graph_builder import AgentGraphBuilder
from app.agent.interface import BaseAgent
from app.agent.memory.config import MemoryConfig
from app.agent.memory.manager import MemoryManager
from app.agent.model_provider import get_model
from app.agent.tools import get_tools
from app.agent.utils.log import shorten_for_log
from app.schemas.chat import AgentInput
from app.schemas.chat_settings import ChatSettings


logger = logging.getLogger(__name__)


class MyAgent(BaseAgent):
    def __init__(self, chat_settings: ChatSettings):
        self.chat_settings = chat_settings
        self.tools = get_tools(self.chat_settings.tools_list)
        self.model = get_model(chat_settings)
        if self.tools:
            # 仅在存在工具时绑定，避免无工具会话引入额外协议开销。
            self.model = self.model.bind_tools(self.tools)

        # LangGraph 运行配置：thread_id 用于 checkpoint 与状态隔离。
        self.config: RunnableConfig = {
            "configurable": {
                "thread_id": chat_settings.session_id,
            }
        }

        self.checkpoint_repo = CheckpointRepository()
        self.memory_manager = MemoryManager(
            session_id=chat_settings.session_id,
            config=MemoryConfig.from_env(),
            chat_settings=chat_settings,
        )

        # 图构建由独立 builder 负责，MyAgent 只做装配。
        self.graph_builder = AgentGraphBuilder(
            model=self.model,
            tools=self.tools,
            chat_settings=self.chat_settings,
            memory_manager=self.memory_manager,
        )
        self.graph: Any | None = None
        # 首次记录基线，用于失败时回滚本轮新增 checkpoint。
        self.checkpoint_watermark = self.checkpoint_repo.get_thread_checkpoint_watermark(
            chat_settings.session_id
        )

    async def ainvoke_agent_stream(self, user_message: AgentInput) -> AsyncIterator[AIMessage | AIMessageChunk]:
        """流式调用入口：仅透传 chatbot 节点的 AI 输出分片。"""
        if self.graph is None:
            self.graph = await self.graph_builder.build()

        active_session_id = self.chat_settings.session_id
        logger.info("[Agent][session=%s] 收到流式消息: %s", active_session_id, shorten_for_log(user_message.message))

        # 每轮前刷新水位线，确保回滚范围只覆盖当前轮次写入。
        self.checkpoint_watermark = self.checkpoint_repo.get_thread_checkpoint_watermark(active_session_id)

        async for chunk, metadata in self.graph.astream(
            cast(Any, {
                # 新回合输入只注入当前用户消息；记忆字段显式清空，防止跨回合复用。
                "messages": [HumanMessage(content=user_message.message)],
                "session_id": active_session_id,
                "memory_text": None,
            }),
            config=self.config,
            stream_mode="messages",
        ):
            node_name = metadata.get("langgraph_node") if isinstance(metadata, dict) else None
            # 只向上游暴露 chatbot 的自然语言输出，屏蔽工具/记忆节点中间事件。
            if node_name != "chatbot":
                continue
            if isinstance(chunk, (AIMessage, AIMessageChunk)):
                yield chunk

    def rollback_thread_checkpoints(self, checkpoint_ns: str = "") -> tuple[int, int]:
        """回滚本轮会话中基线之后写入的 checkpoint。"""
        # 回滚细节下沉到 repository，保持 Agent 接口稳定且可替换。
        return self.checkpoint_repo.rollback_thread_checkpoints(
            thread_id=self.chat_settings.session_id,
            baseline_rowid=self.checkpoint_watermark,
            checkpoint_ns=checkpoint_ns,
        )
