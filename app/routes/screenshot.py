"""截屏确认路由"""
import logging

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.dependencies import get_agent_service
from app.services.agent_service import AgentService
from app.utils.sse_formatter import SSEFormatter


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/screenshot", tags=["screenshot"])


class ScreenshotResponseRequest(BaseModel):
    """用户响应截屏请求的请求体"""
    session_id: str
    approved: bool


@router.post("/respond")
async def respond_to_screenshot(
    payload: ScreenshotResponseRequest,
    agent_service: AgentService = Depends(get_agent_service),
) -> StreamingResponse:
    """用户响应截屏请求，恢复对话执行。

    用户确认后，Agent 会继续执行截屏工具并返回结果。

    Args:
        payload: 包含 session_id 和 approved 的请求体

    Returns:
        SSE 流式响应，格式与 /chat 相同
    """
    logger.info("[ScreenshotRoute] 收到截屏响应: session_id=%s, approved=%s",
                payload.session_id, payload.approved)

    async def event_stream():
        formatter = SSEFormatter()
        try:
            async for event in agent_service.resume_after_screenshot(
                payload.session_id,
                payload.approved
            ):
                formatted = formatter.format(event)
                if formatted:
                    yield formatted
            yield formatter.done()
        except Exception as e:
            logger.exception("[ScreenshotRoute] 恢复对话失败")
            yield formatter.error(str(e))

    return StreamingResponse(event_stream(), media_type="text/event-stream")
