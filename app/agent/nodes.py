import logging
import uuid
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage, BaseMessage

from app.agent.memory.manager import MemoryManager
from app.agent.state import (
    AgentState,
    RECENT_CONTEXT_HUMAN_MESSAGES,
    SCREENSHOT_MESSAGE_NAME,
    SUMMARY_EVERY_HUMAN_MESSAGES,
    is_screenshot_message,
)
from app.agent.tools.screenshot import SCREENSHOT_SUCCESS_PREFIX
from app.agent.utils.background_tasks import create_background_task
from app.agent.utils.image_utils import (
    ImageTaskResult,
    clear_task,
    get_cache_key,
    get_image_task,
    has_image_content,
)
from app.agent.utils.llm_utils import ainvoke_with_retry
from app.agent.utils.messages import normalize_messages_for_model
from app.agent.utils.text_utils import extract_text, get_last_human_text, split_context
from app.agent.utils.work_memory import slice_recent_messages_by_human
from app.schemas.chat_settings import ChatSettings

logger = logging.getLogger(__name__)


class ChatNode:
    def __init__(self, model: Any, chat_settings: ChatSettings, memory_manager: MemoryManager):
        self.model = model
        self.chat_settings = chat_settings
        self.memory_manager = memory_manager

    async def __call__(self, state: AgentState) -> dict[str, Any]:
        # 先裁剪上下文窗口，再做消息格式清洗，控制 token 并避免格式异常。
        recent_messages = slice_recent_messages_by_human(
            state.messages,
            max_human_messages=RECENT_CONTEXT_HUMAN_MESSAGES,
        )
        recent_messages = normalize_messages_for_model(recent_messages)

        # 本轮首次进入 chatbot 才检索记忆，避免工具回环阶段重复查询。
        if state.memory_text is None:
            last_user_text = get_last_human_text(recent_messages)

            # 新系统：get_context() 返回已格式化的上下文，直接使用
            state.memory_text = await self.memory_manager.get_context(query=last_user_text)

        # 动态拼装系统提示词：基础 prompt + 记忆上下文
        system_prompt = self.chat_settings.system_prompt
        if state.memory_text:
            system_prompt = f"{system_prompt}\n\n以下文本是你的记忆，其中，[你的历史日记和摘要]是你对前段时间和当前对话的记忆，[相关情景记忆]和[相关语义知识]是系统根据用户输入检索到的，你记忆的更早之前的事情。\n\n{state.memory_text}"

        messages = [SystemMessage(content=system_prompt)] + recent_messages
        response = await ainvoke_with_retry(self.model, messages)
        return {
            "messages": [response],
            "memory_text": state.memory_text,
        }


class MemoryFinalizeNode:
    def __init__(self, chat_settings: ChatSettings, memory_manager: MemoryManager):
        self.chat_settings = chat_settings
        self.memory_manager = memory_manager

    async def __call__(self, state: AgentState) -> dict[str, Any]:
        # 通过计数器控制记忆提取频率
        next_counter = state.summary_counter + 1

        # 获取最后一条 HumanMessage，检查是否有图片描述任务
        updated_message = None
        image_description = None
        image_filenames: list[str] | None = None

        for msg in reversed(state.messages):
            if isinstance(msg, HumanMessage) and not is_screenshot_message(msg):
                # 检查是否有图片
                if has_image_content(msg.content) and msg.id:
                    cache_key = get_cache_key(state.session_id, msg.id)

                    # 从缓存获取任务
                    task = get_image_task(cache_key)
                    if task is not None:
                        # 等待任务完成
                        try:
                            result: ImageTaskResult = await task
                            image_description = result.description
                            image_filenames = result.filenames
                            logger.info("[MemoryFinalize] 获取图片描述: %s", image_description)
                        except Exception as e:
                            logger.warning("[MemoryFinalize] 图片描述任务失败: %s", e)
                            image_description = "图片"
                            image_filenames = []
                        finally:
                            # 清理任务引用
                            clear_task(cache_key)

                    # 如果有描述，创建新的消息（带 additional_kwargs）
                    if image_description:
                        updated_message = HumanMessage(
                            content=msg.content,
                            id=msg.id,
                            additional_kwargs={
                                "image_description": image_description,
                                "image_filenames": image_filenames,
                            },
                        )
                elif has_image_content(msg.content):
                    # 有图片但没有 id，无法创建 updated_message 来更新 additional_kwargs
                    # 但 image_description 仍会传给 try_summary 保存到数据库
                    image_description = "图片"
                    image_filenames = []
                    logger.warning("[MemoryFinalize] 消息有图片但没有 id，聊天记录将使用占位符")
                break

        # 构建返回值
        result: dict[str, Any] = {"summary_counter": next_counter}

        # 如果有更新的消息，返回它（LangGraph 的 add_messages reducer 会更新原消息）
        if updated_message is not None:
            result["messages"] = [updated_message]

        # try_summary 每轮都触发（保存对话 + 检查摘要/日记）
        # 传递 image_description 和 image_filenames 参数
        create_background_task(
            self._try_summary(list(state.messages), image_description, image_filenames),
            logger=logger,
            task_name="memory_finalize.try_summary",
        )

        # add() 每 10 轮触发一次（情景记忆 + 语义记忆提取）
        if next_counter >= SUMMARY_EVERY_HUMAN_MESSAGES:
            # 构建消息列表：如果有更新的消息，替换原消息
            messages_for_persist = list(state.messages)
            if updated_message is not None and updated_message.id:
                # 找到并替换原消息
                for i, msg in enumerate(messages_for_persist):
                    if isinstance(msg, HumanMessage) and msg.id == updated_message.id:
                        messages_for_persist[i] = updated_message
                        break

            create_background_task(
                self._persist_memory(messages_for_persist),
                logger=logger,
                task_name="memory_finalize.persist",
            )
            result["summary_counter"] = 0

        return result

    async def _try_summary(
        self,
        messages: list[Any],
        image_description: str | None = None,
        image_filenames: list[str] | None = None,
    ) -> None:
        """每轮触发：保存对话并检查摘要/日记

        Args:
            messages: 消息列表
            image_description: 图片描述（如果有图片）
            image_filenames: 图片文件名列表（如果有图片）
        """
        last_human = get_last_human_text(messages)
        last_ai = None
        for msg in reversed(messages):
            if msg.type == "ai":
                last_ai = extract_text(msg.content)
                break

        if last_human and last_ai:
            await self.memory_manager.try_summary(last_human, last_ai, image_description, image_filenames)

    async def _persist_memory(self, messages: list[Any]) -> None:
        """每 10 轮触发：提取情景记忆和语义记忆"""
        # 使用 split_context 提取前情提要和待提取消息
        history_messages, recent_messages = split_context(
            messages,
            later_human_count=10,      # 10对待提取
            previous_human_count=5,    # 5对前情提要
        )

        # 添加情景记忆和语义记忆
        await self.memory_manager.add(recent_messages, history_messages)


class ScreenshotNode:
    """处理截屏工具返回结果，注入特殊 HumanMessage

    当截屏工具成功执行后，创建携带图片的特殊 HumanMessage。
    旧截图的压缩由 state reducer 中的 compress_screenshot_messages 处理。
    """

    async def __call__(self, state: AgentState) -> dict[str, Any]:
        messages = state.messages

        # 检查最后一条 ToolMessage 是否为截屏成功
        for msg in reversed(messages):
            if isinstance(msg, ToolMessage) and isinstance(msg.content, str) and msg.content.startswith(SCREENSHOT_SUCCESS_PREFIX):
                # 提取图片数据
                image_data = msg.content[len(SCREENSHOT_SUCCESS_PREFIX):]

                # 构建返回的消息列表
                result_messages: list[BaseMessage] = []

                # 更新 ToolMessage 内容（移除图片数据，只保留成功标记）
                # 继承原消息 id 以便 add_messages reducer 正确替换
                updated_tool_msg = ToolMessage(
                    content="截屏成功",
                    tool_call_id=msg.tool_call_id,
                    name=msg.name,
                    id=msg.id if hasattr(msg, "id") else None
                )
                result_messages.append(updated_tool_msg)

                # 创建特殊 HumanMessage 携带截图
                screenshot_msg = HumanMessage(
                    content=[
                        {"type": "text", "text": "[系统消息]屏幕截图: "},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_data}"}}
                    ],
                    name=SCREENSHOT_MESSAGE_NAME,
                    id=str(uuid.uuid4())
                )
                result_messages.append(screenshot_msg)

                logger.info("[ScreenshotNode] 注入截图消息，图片大小: %d bytes", len(image_data))
                return {"messages": result_messages}

        # 没有截屏成功，直接返回空
        return {}
