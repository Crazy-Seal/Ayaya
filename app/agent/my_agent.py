import asyncio
import json
import logging
import uuid
from typing import AsyncIterator, Any, cast, Union

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.types import Command, Interrupt

from app.agent.checkpoint_repository import CheckpointRepository
from app.agent.graph_builder import AgentGraphBuilder
from app.agent.interface import BaseAgent
from app.agent.memory.config import MemoryConfig
from app.agent.memory.manager import MemoryManager
from app.agent.model_provider import get_model
from app.agent.tools import get_tools
from app.agent.utils.image_description import generate_multiple_image_descriptions
from app.agent.utils.image_utils import (
    clear_task,
    extract_text_from_content,
    get_cache_key,
    set_image_task,
)
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

    def _build_human_message(self, user_input: AgentInput) -> HumanMessage:
        """构建 HumanMessage，支持多模态内容。

        Args:
            user_input: 用户输入，包含 message 和可选的 images 列表

        Returns:
            HumanMessage：纯文本或多模态内容
        """
        message_id = str(uuid.uuid4())

        if not user_input.images:
            # 无图片，返回纯文本消息
            return HumanMessage(content=user_input.message, id=message_id)

        # 有图片，构建多模态消息
        content: list[dict] = []
        # 添加所有图片
        for image_data in user_input.images:
            content.append({"type": "image_url", "image_url": {"url": image_data}})
        # 添加文本
        content.append({"type": "text", "text": user_input.message})
        return HumanMessage(content=content, id=message_id)

    async def ainvoke_agent_stream(
        self, user_message: AgentInput
    ) -> AsyncIterator[AIMessage | AIMessageChunk | Interrupt]:
        """流式调用入口：透传 chatbot 节点的 AI 输出分片和 interrupt 事件。

        Yields:
            AIMessage | AIMessageChunk: 正常的 AI 消息
            Interrupt: 当发生 interrupt 时（如截屏确认）
        """
        if self.graph is None:
            self.graph = await self.graph_builder.build()

        active_session_id = self.chat_settings.session_id
        logger.info("[Agent][session=%s] 收到流式消息: %s", active_session_id, shorten_for_log(user_message.message))

        # 每轮前刷新水位线，确保回滚范围只覆盖当前轮次写入。
        self.checkpoint_watermark = self.checkpoint_repo.get_thread_checkpoint_watermark(active_session_id)

        # 构建多模态消息
        human_msg = self._build_human_message(user_message)

        # 如果有图片，启动后台任务生成图片描述
        if user_message.images and human_msg.id:
            cache_key = get_cache_key(active_session_id, human_msg.id)
            # 清理可能存在的旧任务
            clear_task(cache_key)
            # 启动后台任务
            context = extract_text_from_content(human_msg.content)
            task = asyncio.create_task(
                generate_multiple_image_descriptions(
                    images=user_message.images,
                    context=context,
                    max_length=200,
                )
            )
            set_image_task(cache_key, task)
            logger.info("[Agent][session=%s] 启动图片描述生成任务: key=%s", active_session_id, cache_key)

        # 使用 messages + updates 模式，并启用 v2 版本以正确检测 interrupt
        async for chunk in self.graph.astream(
            cast(Any, {
                # 新回合输入只注入当前用户消息；记忆字段显式清空，防止跨回合复用。
                "messages": [human_msg],
                "session_id": active_session_id,
                "memory_text": None,
            }),
            config=self.config,
            stream_mode=["messages", "updates"],
            version="v2",
        ):

            chunk_type = chunk.get("type")
            chunk_data = chunk.get("data")
            logger.debug(
                "[Agent][session=%s] chunk_type=%s, chunk_data_type=%s",
                active_session_id,
                chunk_type,
                type(chunk_data).__name__,
            )

            if chunk_type == "messages":
                # 消息类型：检查是否来自 chatbot 节点
                if isinstance(chunk_data, tuple) and len(chunk_data) == 2:
                    msg, metadata = chunk_data
                    node_name = metadata.get("langgraph_node") if isinstance(metadata, dict) else None
                    if node_name == "chatbot" and isinstance(msg, (AIMessage, AIMessageChunk)):
                        yield msg
                    # else: 跳过非 chatbot 节点的消息（如 tools, screenshot_handler）
                else:
                    # 调试日志：记录非预期格式的 chunk
                    logger.warning(
                        "[Agent][session=%s] messages chunk 格式非预期: type=%s, value=%s",
                        active_session_id,
                        type(chunk_data).__name__,
                        repr(chunk_data)[:200] if chunk_data else None,
                    )

            elif chunk_type == "updates":
                # 更新类型：检查是否包含 interrupt
                if isinstance(chunk_data, dict) and "__interrupt__" in chunk_data:
                    interrupts = chunk_data["__interrupt__"]
                    if interrupts:
                        # 返回第一个 interrupt
                        yield interrupts[0]

                # 检查是否包含 chatbot 节点的工具调用
                if isinstance(chunk_data, dict) and "chatbot" in chunk_data:
                    chatbot_update = chunk_data["chatbot"]
                    if isinstance(chatbot_update, dict) and "messages" in chatbot_update:
                        messages = chatbot_update["messages"]
                        last_msg = messages[-1]
                        if hasattr(last_msg, "tool_calls") and last_msg.tool_calls:
                            for tc in last_msg.tool_calls:
                                tool_name = tc.get("name", "未知工具")
                                yield f"__TOOL_CALL__:{json.dumps({'tool_name': tool_name}, ensure_ascii=False)}"

    def rollback_thread_checkpoints(self, checkpoint_ns: str = "") -> tuple[int, int]:
        """回滚本轮会话中基线之后写入的 checkpoint。"""
        # 回滚细节下沉到 repository，保持 Agent 接口稳定且可替换。
        return self.checkpoint_repo.rollback_thread_checkpoints(
            thread_id=self.chat_settings.session_id,
            baseline_rowid=self.checkpoint_watermark,
            checkpoint_ns=checkpoint_ns,
        )

    async def resume_with_command(
        self,
        command: Command
    ) -> AsyncIterator[AIMessage | AIMessageChunk | Interrupt]:
        """使用 Command 恢复中断的对话。

        当 LangGraph 执行遇到 interrupt 时，图会暂停并保存状态。
        通过传入 Command(resume=...) 可以恢复执行，resume 的值会成为
        interrupt() 函数的返回值。

        Args:
            command: 包含 resume 数据的 Command 对象

        Yields:
            AI 消息或消息分片，或 Interrupt 对象
        """
        if self.graph is None:
            self.graph = await self.graph_builder.build()

        active_session_id = self.chat_settings.session_id
        logger.info("[Agent][session=%s] 恢复中断的对话", active_session_id)

        # 恢复前刷新水位线
        self.checkpoint_watermark = self.checkpoint_repo.get_thread_checkpoint_watermark(active_session_id)

        async for chunk in self.graph.astream(
            command,
            config=self.config,
            stream_mode=["messages", "updates"],
            version="v2",
        ):
            if not isinstance(chunk, dict):
                continue

            chunk_type = chunk.get("type")
            chunk_data = chunk.get("data")

            if chunk_type == "messages":
                if isinstance(chunk_data, tuple) and len(chunk_data) == 2:
                    msg, metadata = chunk_data
                    node_name = metadata.get("langgraph_node") if isinstance(metadata, dict) else None
                    if node_name == "chatbot" and isinstance(msg, (AIMessage, AIMessageChunk)):
                        yield msg

            elif chunk_type == "updates":
                # 检查是否包含后续 interrupt（如连续截屏）
                if isinstance(chunk_data, dict) and "__interrupt__" in chunk_data:
                    interrupts = chunk_data["__interrupt__"]
                    if interrupts:
                        yield interrupts[0]

                # 检查是否包含 chatbot 节点的工具调用
                if isinstance(chunk_data, dict) and "chatbot" in chunk_data:
                    chatbot_update = chunk_data["chatbot"]
                    if isinstance(chatbot_update, dict) and "messages" in chatbot_update:
                        messages = chatbot_update["messages"]
                        last_msg = messages[-1]
                        if hasattr(last_msg, "tool_calls") and last_msg.tool_calls:
                            for tc in last_msg.tool_calls:
                                tool_name = tc.get("name", "未知工具")
                                yield f"__TOOL_CALL__:{json.dumps({'tool_name': tool_name}, ensure_ascii=False)}"
