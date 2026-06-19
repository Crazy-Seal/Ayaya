"""AgentService 

产出 AgentEvent（非 DONE/ERROR）；DONE 交给路由的 done() 收尾，ERROR 转为异常
"""

import logging
from datetime import datetime
from typing import AsyncIterator, Callable

from app.agent.agent import Agent
from app.agent.core.event_router import EventType
from app.crud.chat_history_dao import ChatHistoryDao
from app.schemas.chat import AgentInput
from app.schemas.chat_settings import ChatSettings
from app.services.agent_factory import build_agent

logger = logging.getLogger(__name__)

# 中断后恢复用的活跃 agent 缓存
_active_agents: dict[str, Agent] = {}


class AgentService:
    def __init__(
        self,
        chat_history_dao: ChatHistoryDao,
        chat_settings_loader: Callable[[str], ChatSettings],
        agent_factory: Callable[[ChatSettings], Agent] | None = None,
    ):
        self.chat_history_dao = chat_history_dao
        self.chat_settings_loader = chat_settings_loader
        self.agent_factory = agent_factory or build_agent

    @staticmethod
    def _build_timed_user_message(user_message: str) -> str:
        now = datetime.now().astimezone()
        weekday_text = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"][now.weekday()]
        now_text = f"{now.strftime('%Y-%m-%d %H:%M:%S %z')} {weekday_text}"
        return f"[{now_text}] {user_message}"

    def get_health_data(self, session_id: str) -> dict[str, str]:
        chat_settings = self.chat_settings_loader(session_id)
        return {"status": "ok", "model": chat_settings.model_name}

    async def _close(self, session_id: str, agent: Agent) -> None:
        _active_agents.pop(session_id, None)
        try:
            await agent.close()
        except Exception:
            logger.exception("[AgentService][session=%s] 关闭 agent 失败", session_id)
    async def stream_chat(self, agent_input: AgentInput, session_id: str = "default") -> AsyncIterator:
        chat_settings = self.chat_settings_loader(session_id)
        agent = self.agent_factory(chat_settings)
        _active_agents[session_id] = agent

        message = self._build_timed_user_message(agent_input.message)
        interrupted = False
        try:
            async for event in agent.run(message, images=agent_input.images):
                if event.type == EventType.ERROR:
                    raise RuntimeError(event.data)
                if event.type == EventType.DONE:
                    continue
                if event.type == EventType.INTERRUPT:
                    interrupted = True
                yield event
        except Exception as e:
            logger.exception("[AgentService][session=%s] 运行出错: %s", session_id, e)
            await self._close(session_id, agent)
            raise RuntimeError(f"Agent 执行出错: {e}") from e

        # 中断时保留 agent 供恢复；否则关闭释放连接
        if not interrupted:
            await self._close(session_id, agent)

    async def resume_after_screenshot(
        self,
        session_id: str,
        approved: bool,
        screenshot_data: str | None = None,
        width: int | None = None,
        height: int | None = None,
    ) -> AsyncIterator:
        agent = _active_agents.get(session_id)
        if not agent:
            raise RuntimeError("会话已过期")

        resume_data: dict = {"approved": approved}
        if screenshot_data:
            resume_data["screenshot_data"] = screenshot_data
        if width is not None:
            resume_data["width"] = width
        if height is not None:
            resume_data["height"] = height

        interrupted = False
        try:
            async for event in agent.resume(resume_data):
                if event.type == EventType.ERROR:
                    raise RuntimeError(event.data)
                if event.type == EventType.DONE:
                    continue
                if event.type == EventType.INTERRUPT:
                    interrupted = True
                yield event
        except Exception as e:
            logger.exception("[AgentService][session=%s] 恢复对话失败", session_id)
            await self._close(session_id, agent)
            raise RuntimeError(f"恢复对话失败: {e}") from e

        if not interrupted:
            await self._close(session_id, agent)
