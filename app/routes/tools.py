from fastapi import APIRouter

from app.schemas.result import Result
from app.schemas.tool import ToolInfo

router = APIRouter(tags=["tools"])


@router.get("/tools", response_model=Result)
def list_tools() -> Result:
    """获取所有可用工具的名称和描述"""
    from app.agent.tools.registry import ToolRegistry

    tool_list: list[ToolInfo] = []
    for name in ToolRegistry.list_tools():
        tool_class = ToolRegistry.get(name)
        if tool_class is None:
            continue
        description = getattr(tool_class, "description", "") or ""
        tool_list.append(ToolInfo(name=tool_class.name, description=description.strip()))

    return Result(data={"tools": [t.model_dump() for t in tool_list]}, msg="success", code=200)
