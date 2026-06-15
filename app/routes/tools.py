from fastapi import APIRouter

from app.schemas.result import Result
from app.schemas.tool import ToolInfo

router = APIRouter(tags=["tools"])


def _list_tools_v2() -> list[ToolInfo]:
    """从 ToolRegistry 解析每个工具类读取类属性。"""
    from app.agent.tools.registry import ToolRegistry

    infos: list[ToolInfo] = []
    for name in ToolRegistry.list_tools():
        tool_class = ToolRegistry.get(name)
        if tool_class is None:
            continue
        description = getattr(tool_class, "description", "") or ""
        infos.append(ToolInfo(name=tool_class.name, description=description.strip()))
    return infos


@router.get("/tools", response_model=Result)
def list_tools() -> Result:
    """获取所有可用工具的名称和描述"""
    tool_list = _list_tools_v2()
    return Result(data={"tools": [t.model_dump() for t in tool_list]}, msg="success", code=200)
