from fastapi import APIRouter, Depends, Query

from app.dependencies import get_chat_history_service
from app.schemas.chat import ChatHistoryItem
from app.schemas.result import Result
from app.services.chat_history_service import ChatHistoryService

router = APIRouter(tags=["memory"])


@router.get("/chat_history/{session_id}")
async def list_chat_history(
    session_id: str,
    start: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1),
    chat_history_service: ChatHistoryService = Depends(get_chat_history_service),
) -> Result:
    history = await chat_history_service.get_chat_history_data(session_id=session_id, start=start, limit=limit)
    items = [ChatHistoryItem(**item).model_dump() for item in history]
    return Result(data=items, msg="success", code=200)


@router.get("/chat_history_last_n/{session_id}")
async def list_chat_history_last_n(
    session_id: str,
    n: int = Query(default=100, ge=1, le=500),
    chat_history_service: ChatHistoryService = Depends(get_chat_history_service),
) -> Result:
    """获取会话最后 N 条历史记录，按时间升序返回。"""
    history = await chat_history_service.get_chat_history_last_n(session_id=session_id, n=n)
    items = [ChatHistoryItem(**item).model_dump() for item in history]
    return Result(data=items, msg="success", code=200)

