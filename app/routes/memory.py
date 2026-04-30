from fastapi import APIRouter, Depends, Query

from app.dependencies import get_memory_service
from app.schemas.chat import ChatHistoryItem
from app.schemas.result import Result
from app.services.memory_service import MemoryService

router = APIRouter(tags=["memory"])


@router.get("/chat_history/{session_id}")
async def list_chat_history(
    session_id: str,
    start: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1),
    memory_service: MemoryService = Depends(get_memory_service),
) -> Result:
    history = await memory_service.get_chat_history_data(session_id=session_id, start=start, limit=limit)
    items = [ChatHistoryItem(**item).model_dump() for item in history]
    return Result(data=items, msg="success", code=200)


@router.get("/chat_history_last_n/{session_id}")
async def list_chat_history_last_n(
    session_id: str,
    n: int = Query(default=100, ge=1, le=500),
    memory_service: MemoryService = Depends(get_memory_service),
) -> Result:
    """获取会话最后 N 条历史记录，按时间升序返回。"""
    history = await memory_service.get_chat_history_last_n(session_id=session_id, n=n)
    items = [ChatHistoryItem(**item).model_dump() for item in history]
    return Result(data=items, msg="success", code=200)

