from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition

from app.agent.checkpoint_repository import get_checkpointer
from app.agent.memory.memory import get_store
from app.agent.nodes import ChatNode, MemoryFinalizeNode
from app.agent.state import AgentState
from app.schemas.chat_settings import ChatSettings


class AgentGraphBuilder:
    """Build the LangGraph topology from pluggable node implementations."""

    def __init__(self, model: Any, tools: list[Any], chat_settings: ChatSettings):
        self.model = model
        self.tools = tools
        self.chat_settings = chat_settings

    def build(self) -> Any:
        # 节点实现与图结构分离，便于后续替换节点策略。
        chat_node = ChatNode(model=self.model, chat_settings=self.chat_settings)
        memory_node = MemoryFinalizeNode(chat_settings=self.chat_settings)

        def chatbot(state: AgentState, config: RunnableConfig):
            return chat_node(state)

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

        # 编译时统一注入 checkpoint 与向量存储，避免节点层重复依赖装配。
        return builder.compile(checkpointer=get_checkpointer(), store=get_store())
