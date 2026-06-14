import os
from typing import Callable

from fastapi import Depends
from functools import lru_cache

from app.crud.chat_history_dao import ChatHistoryDao
from app.crud.chat_settings_dao import ChatSettingsDao
from app.schemas.chat_settings import ChatSettings
from app.services.agent_service import AgentService
from app.services.chat_settings_service import ChatSettingsService
from app.services.chat_history_service import ChatHistoryService


@lru_cache(maxsize=1)
def get_chat_history_dao() -> ChatHistoryDao:
    return ChatHistoryDao()


@lru_cache(maxsize=1)
def get_chat_settings_dao() -> ChatSettingsDao:
    return ChatSettingsDao()


def get_chat_settings_loader(
    chat_settings_dao: ChatSettingsDao = Depends(get_chat_settings_dao),
) -> Callable[[str], ChatSettings]:
    """提供配置加载函数，用于 AgentService 等需要延迟加载的场景"""
    return chat_settings_dao.get_chat_settings


@lru_cache(maxsize=1)
def get_agent_service(
    chat_history_dao: ChatHistoryDao = Depends(get_chat_history_dao),
    chat_settings_loader: Callable[[str], ChatSettings] = Depends(get_chat_settings_loader),
):
    """按 AGENT_BACKEND 选择 v1(默认) 或 v2 后端。v1 路径完全不触及 agent_v2。"""
    backend = os.getenv("AGENT_BACKEND", "v1").strip().lower()
    if backend == "v2":
        from app.services.agent_service_v2 import AgentServiceV2
        return AgentServiceV2(
            chat_history_dao=chat_history_dao,
            chat_settings_loader=chat_settings_loader,
        )
    return AgentService(
        chat_history_dao=chat_history_dao,
        chat_settings_loader=chat_settings_loader,
    )


@lru_cache(maxsize=1)
def get_chat_settings_service(
    chat_settings_dao: ChatSettingsDao = Depends(get_chat_settings_dao),
) -> ChatSettingsService:
    return ChatSettingsService(chat_settings_dao=chat_settings_dao)


@lru_cache(maxsize=1)
def get_chat_history_service(chat_history_dao: ChatHistoryDao = Depends(get_chat_history_dao)) -> ChatHistoryService:
    return ChatHistoryService(chat_history_dao=chat_history_dao)
