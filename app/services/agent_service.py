import json
import logging
from datetime import datetime
from typing import AsyncIterator, Callable, Union

from langchain_core.messages import AIMessage, AIMessageChunk
from langgraph.types import Command, Interrupt

from app.agent.my_agent import MyAgent
from app.agent.interface import BaseAgent
from app.config.config import get_chat_settings
from app.crud.chat_history_dao import ChatHistoryDao
from app.schemas.chat import AgentInput
from app.schemas.chat_settings import ChatSettings


logger = logging.getLogger(__name__)

# 存储 agent 实例供中断后恢复使用
_active_agents: dict[str, BaseAgent] = {}


class AgentService:
    def __init__(
        self,
        chat_history_dao: ChatHistoryDao,
        chat_settings_loader: Callable[[str], ChatSettings] = get_chat_settings,
        agent_factory: Callable[[ChatSettings], BaseAgent] | None = None,
    ):
        self.chat_history_dao = chat_history_dao
        self.chat_settings_loader = chat_settings_loader
        self.agent_factory = agent_factory or MyAgent

    @staticmethod
    def _build_timed_user_message(user_message: str) -> str:
        """将本地时间写入用户消息，便于模型感知时间演进。"""
        now = datetime.now().astimezone()
        weekday_text = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"][now.weekday()]
        now_text = f"{now.strftime('%Y-%m-%d %H:%M:%S %z')} {weekday_text}"
        return f"[{now_text}] {user_message}"

    @staticmethod
    def _extract_text(content: object) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, dict):
            text = content.get("text")
            if isinstance(text, str):
                return text
            return ""
        if isinstance(content, list):
            text_parts: list[str] = []
            for item in content:
                if isinstance(item, str):
                    text_parts.append(item)
                    continue
                if isinstance(item, dict):
                    text = item.get("text")
                    if isinstance(text, str):
                        text_parts.append(text)
            return "".join(text_parts)

        # 兼容 LangChain message/chunk 的 text 属性/方法
        if hasattr(content, "text"):
            text_attr = getattr(content, "text")
            if isinstance(text_attr, str):
                return text_attr
            if callable(text_attr):  # 兼容旧版 .text()
                text = text_attr()
                if isinstance(text, str):
                    return text
        return ""

    def get_health_data(self, session_id: str) -> dict[str, str]:
        chat_settings = self.chat_settings_loader(session_id)
        return {
            "status": "ok",
            "model": chat_settings.model_name,
        }

    @staticmethod
    def _rollback_checkpoints(session_id: str, agent: BaseAgent) -> tuple[int, int]:
        try:
            return agent.rollback_thread_checkpoints()
        except Exception:
            logger.exception("[AgentService][session=%s] checkpoints回滚失败", session_id)
            return 0, 0

    async def stream_chat(self, agent_input: AgentInput, session_id: str = "default") -> AsyncIterator[str]:
        chat_settings = self.chat_settings_loader(session_id)
        agent = self.agent_factory(chat_settings)

        # 缓存 agent 实例供中断后恢复使用
        _active_agents[session_id] = agent

        timed_agent_input = AgentInput(
            message=self._build_timed_user_message(agent_input.message),
            images=agent_input.images,
        )

        response_parts: list[str] = []
        chunk_count = 0
        try:
            async for chunk in agent.ainvoke_agent_stream(timed_agent_input):
                chunk_count += 1
                # 检查是否是工具调用事件
                if isinstance(chunk, str) and chunk.startswith("__TOOL_CALL__:"):
                    yield chunk
                    continue

                # 检查是否是 Interrupt 类型
                if isinstance(chunk, Interrupt):
                    # Interrupt 包含 value 字段，是传给 interrupt() 的参数
                    interrupt_data = {
                        "value": chunk.value if hasattr(chunk, 'value') else str(chunk)
                    }
                    # 发送特殊标记，让路由层转换为 SSE 事件
                    yield f"__INTERRUPT__:{json.dumps(interrupt_data, ensure_ascii=False, default=str)}"
                    return

                # 正常消息处理
                if isinstance(chunk, (AIMessage, AIMessageChunk)):
                    text = self._extract_text(chunk.content)
                    if not text:
                        text = self._extract_text(chunk)
                    if not text:
                        continue
                    response_parts.append(text)
                    yield text
                else:
                    logger.warning(
                        "[AgentService][session=%s] 收到非 AI 消息类型: %s",
                        session_id,
                        type(chunk).__name__,
                    )
        except Exception:
            logger.exception("[AgentService][session=%s] graph运行中出现错误，尝试回滚checkpoints", session_id)
            self._rollback_checkpoints(session_id, agent)
            yield "[错误：agent调用异常]"
            return

        ai_message = "".join(response_parts)
        logger.info("[AgentService][session=%s] 收到 %d 个 chunks, 提取文本长度: %d", session_id, chunk_count, len(ai_message))
        if not ai_message.strip():
            deleted_checkpoints, deleted_writes = self._rollback_checkpoints(session_id, agent)
            logger.warning(
                "[AgentService][session=%s] 模型输出空，已回滚 checkpoints=%d, writes=%d",
                session_id,
                deleted_checkpoints,
                deleted_writes,
            )
            yield "[错误：未返回内容]"
            return

    async def resume_after_screenshot(
        self,
        session_id: str,
        approved: bool
    ) -> AsyncIterator[str]:
        """用户确认截屏后恢复对话。

        Args:
            session_id: 会话 ID
            approved: 用户是否允许截屏

        Yields:
            AI 响应文本
        """
        agent = _active_agents.get(session_id)
        if not agent:
            yield "[系统]会话已过期"
            return

        logger.info("[AgentService][session=%s] 用户确认截屏: approved=%s", session_id, approved)

        # 使用 Command(resume=...) 恢复执行
        command = Command(resume={"approved": approved})

        response_parts: list[str] = []
        chunk_count = 0
        try:
            async for chunk in agent.resume_with_command(command):
                chunk_count += 1
                # 检查是否是工具调用事件
                if isinstance(chunk, str) and chunk.startswith("__TOOL_CALL__:"):
                    yield chunk
                    continue

                # 检查是否是后续 Interrupt（连续截屏）
                if isinstance(chunk, Interrupt):
                    interrupt_data = {
                        "value": chunk.value if hasattr(chunk, 'value') else str(chunk)
                    }
                    yield f"__INTERRUPT__:{json.dumps(interrupt_data, ensure_ascii=False, default=str)}"
                    return

                text = self._extract_text(chunk.content)
                if text:
                    response_parts.append(text)
                    yield text
        except Exception:
            logger.exception("[AgentService][session=%s] 恢复对话失败", session_id)
            self._rollback_checkpoints(session_id, agent)
            yield "[系统]恢复对话失败"
            return

        logger.info("[AgentService][session=%s] 恢复对话完成，收到 %d 个 chunks, 提取文本长度: %d",
                    session_id, chunk_count, len("".join(response_parts)))

        # 检查空输出
        ai_message = "".join(response_parts)
        if not ai_message.strip():
            deleted_checkpoints, deleted_writes = self._rollback_checkpoints(session_id, agent)
            logger.warning(
                "[AgentService][session=%s] 恢复对话后输出空，已回滚 checkpoints=%d, writes=%d",
                session_id,
                deleted_checkpoints,
                deleted_writes,
            )
            yield "[系统]未返回内容"
