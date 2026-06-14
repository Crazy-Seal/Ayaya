import os

from fastapi import APIRouter

from app.schemas.result import Result
from app.schemas.tool import ToolInfo

router = APIRouter(tags=["tools"])


def _list_tools_v1() -> list[ToolInfo]:
    """v1(LangGraph) 工具：get_tools() 返回已实例化的工具对象。"""
    from app.agent.tools import get_tools

    return [
        ToolInfo(
            name=tool.name,
            description=tool.description.strip() if tool.description else "",
        )
        for tool in get_tools()
    ]


def _list_tools_v2() -> list[ToolInfo]:
    """v2(自建框架) 工具：从 ToolRegistry 解析每个工具类读取类属性。"""
    from app.agent_v2.tools.registry import ToolRegistry

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
    """获取所有可用工具的名称和描述。

    按 AGENT_BACKEND 选择 v1(默认) 或 v2 后端，与 /chat 保持一致。
    """
    backend = os.getenv("AGENT_BACKEND", "v1").strip().lower()
    tool_list = _list_tools_v2() if backend == "v2" else _list_tools_v1()
    return Result(data={"tools": [t.model_dump() for t in tool_list]}, msg="success", code=200)
