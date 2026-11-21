from fastapi import APIRouter, Depends, Query

from app.dependencies import get_chat_history_service
from app.schemas.chat import ChatHistoryItem
from app.schemas.result import Result
from app.services.chat_history_service import ChatHistoryService

router = APIRouter(tags=["memory"])


@router.get("/chat_history/{session_id}", responses={500: {"description": "保存的消息与当前消息契约不兼容"}})
async def list_chat_history(
    session_id: str,
    start: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1),
    chat_history_service: ChatHistoryService = Depends(get_chat_history_service),
) -> Result[list[ChatHistoryItem]]:
    """聊天页查询历史时返回可见消息，协议不兼容则明确返回 HTTP 500。

    Args:
        session_id (str): 当前操作所属的会话编号。
        start (int): 可见聊天历史的起始位置，从零开始。
        limit (int): 最多返回的记录数量。
        chat_history_service (ChatHistoryService): 查询界面可见聊天历史的应用服务。

    Returns:
        Result[list[ChatHistoryItem]]: 包含历史消息列表的统一接口结果。

    Raises:
        MessageContractError: 持久消息与当前协议不兼容。
    """
    history = await chat_history_service.get_chat_history_data(
        session_id=session_id, start=start, limit=limit
    )
    items = [ChatHistoryItem.model_validate(item) for item in history]
    return Result[list[ChatHistoryItem]](data=items, msg="success", code=200)


@router.get("/chat_history_last_n/{session_id}", responses={404: {"description": "历史查询终点不存在于当前会话"}, 500: {"description": "保存的消息与当前消息契约不兼容"}})
async def list_chat_history_last_n(
    session_id: str,
    n: int = Query(default=100, ge=1, le=500),
    through_message_id: str | None = Query(default=None, min_length=1),
    chat_history_service: ChatHistoryService = Depends(get_chat_history_service),
) -> Result[list[ChatHistoryItem]]:
    """获取最近 N 条聊天历史，工具完成后可限定查询到对应结果消息。

    Args:
        session_id (str): 当前操作所属的会话编号。
        n (int): 最多返回的可见消息条数。
        through_message_id (str | None): 工具结果消息编号，省略时读取最新历史。
        chat_history_service (ChatHistoryService): 查询界面可见聊天历史的应用服务。

    Returns:
        Result[list[ChatHistoryItem]]: 保持消息顺序的可见历史列表。

    Raises:
        ResourceNotFoundError: 查询终点不属于当前会话或已被删除。
        MessageContractError: 持久消息与当前协议不兼容。
    """
    history = await chat_history_service.get_chat_history_last_n(
        session_id=session_id, n=n, through_message_id=through_message_id,
    )
    items = [ChatHistoryItem.model_validate(item) for item in history]
    return Result[list[ChatHistoryItem]](data=items, msg="success", code=200)
