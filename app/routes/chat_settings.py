from fastapi import APIRouter, Depends, HTTPException

from app.dependencies import get_chat_settings_service, get_runtime_scheduler
from app.agent.runtime.agent_runtime_scheduler import AgentRuntimeScheduler
from app.schemas.chat_settings import ChatSettings
from app.schemas.result import Result
from app.services.chat_settings_service import ChatSettingsService
from app.services.plugin_service import PluginConfigurationError

router = APIRouter(tags=["chat_settings"])


# 增
@router.post("/chat_settings")
async def add_chat_settings(
    chat_settings: ChatSettings,
    chat_settings_service: ChatSettingsService = Depends(get_chat_settings_service),
) -> Result[ChatSettings]:
    """新增模型配置。

    Args:
        chat_settings (ChatSettings): 待新增的模型配置。
        chat_settings_service (ChatSettingsService): 由依赖注入提供的模型配置服务。

    Returns:
        Result[ChatSettings]: 实际保存后的完整会话配置。

    Raises:
        HTTPException: 配置写入发生冲突。
    """
    try:
        saved = await chat_settings_service.add_chat_settings(chat_settings)
    except PluginConfigurationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return Result[ChatSettings](data=saved, msg="success", code=200)


# 删
@router.delete("/chat_settings/{session_id}")
async def delete_chat_settings(
    session_id: str,
    chat_settings_service: ChatSettingsService = Depends(get_chat_settings_service),
    scheduler: AgentRuntimeScheduler = Depends(get_runtime_scheduler),
) -> Result[None]:
    """删除指定会话的模型配置。

    Args:
        session_id (str): 待删除配置的会话 ID。
        chat_settings_service (ChatSettingsService): 由依赖注入提供的模型配置服务。
        scheduler (AgentRuntimeScheduler): 删除成功后安排父会话资源清理。

    Returns:
        Result[None]: 确认配置已删除的空数据响应。

    Raises:
        HTTPException: 指定配置不存在。
    """
    try:
        await chat_settings_service.delete_chat_settings(session_id)
        scheduler.schedule_parent_cleanup(session_id)
        return Result[None](data=None, msg="success", code=200)
    except PluginConfigurationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# 查
@router.get("/chat_settings/{session_id}")
def get_chat_settings(
    session_id: str,
    chat_settings_service: ChatSettingsService = Depends(get_chat_settings_service),
) -> Result[ChatSettings]:
    """设置页或桌面主进程按会话读取已规范化的模型配置。

    Args:
        session_id (str): 当前操作所属的会话编号。
        chat_settings_service (ChatSettingsService): 读取与规范化会话设置的应用服务。

    Returns:
        Result[ChatSettings]: 包含会话配置的统一接口结果。

    Raises:
        HTTPException: 指定会话不存在，返回 HTTP 404。
    """
    try:
        data = chat_settings_service.get_chat_settings_by_session(session_id)
        return Result[ChatSettings](data=data, msg="success", code=200)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# 改
@router.put("/chat_settings")
async def update_chat_settings(
    chat_settings: ChatSettings,
    chat_settings_service: ChatSettingsService = Depends(get_chat_settings_service),
) -> Result[ChatSettings]:
    """更新模型配置及其 MCP 权限。

    Args:
        chat_settings (ChatSettings): 更新后的完整模型配置。
        chat_settings_service (ChatSettingsService): 由依赖注入提供的模型配置服务。

    Returns:
        Result[ChatSettings]: 实际保存后的完整会话配置。

    Raises:
        HTTPException: 指定配置不存在。
    """
    try:
        saved = await chat_settings_service.update_chat_settings(chat_settings)
        return Result[ChatSettings](data=saved, msg="success", code=200)
    except PluginConfigurationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
