from pydantic import BaseModel


class ToolInfo(BaseModel):
    """工具信息 VO"""
    name: str
    description: str
