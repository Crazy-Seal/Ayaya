from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition

from app.agent.checkpoint_repository import get_checkpointer_async
from app.agent.memory_hub import MemoryManager
from app.agent.nodes import ChatNode, MemoryFinalizeNode
from app.agent.state import AgentState
from app.schemas.chat_settings import ChatSettings


class AgentGraphBuilder:
    """根据可插拔节点实现构建 LangGraph 拓扑。"""

    def __init__(
        self,
        model: Any,
        tools: list[Any],
        chat_settings: ChatSettings,
        memory_manager: MemoryManager,
    ):
        # 运行依赖在构造阶段注入，避免节点内部直接创建外部资源。
        self.model = model
        self.tools = tools
        self.chat_settings = chat_settings
        self.memory_manager = memory_manager

    async def build(self) -> Any:
        # 节点实现与图结构分离，便于后续替换节点策略。
        chat_node = ChatNode(
            model=self.model,
            chat_settings=self.chat_settings,
            memory_manager=self.memory_manager,
        )
        memory_node = MemoryFinalizeNode(
            chat_settings=self.chat_settings,
            memory_manager=self.memory_manager,
        )

        async def chatbot(state: AgentState, config: RunnableConfig):
            # 统一在这里把 StateGraph 回调转发给节点对象。
            return await chat_node(state)

        builder = StateGraph(AgentState)
        builder.add_node("chatbot", chatbot)
        builder.add_node("tools", ToolNode(tools=self.tools))
        builder.add_node("memory_finalize", memory_node)

        # 主流程：chatbot -> (可选 tools 循环) -> memory_finalize -> END。
        builder.add_edge(START, "chatbot")
        builder.add_conditional_edges(
            "chatbot",
            tools_condition,
            {"tools": "tools", "__end__": "memory_finalize"},
        )
        builder.add_edge("tools", "chatbot")
        builder.add_edge("memory_finalize", END)

        # 编译时仅注入 checkpoint；工具侧记忆查询统一走 memory_hub 调用链。
        return builder.compile(checkpointer=await get_checkpointer_async())
