from fastapi import APIRouter

from app.agent.tools import get_tools
from app.schemas.result import Result
from app.schemas.tool import ToolInfo

router = APIRouter(tags=["tools"])


@router.get("/tools", response_model=Result)
def list_tools() -> Result:
    """获取所有可用工具的名称和描述"""
    tools = get_tools()
    tool_list = [
        ToolInfo(
            name=tool.name,
            description=tool.description.strip() if tool.description else ""
        )
        for tool in tools
    ]
    return Result(data={"tools": [t.model_dump() for t in tool_list]}, msg="success", code=200)
