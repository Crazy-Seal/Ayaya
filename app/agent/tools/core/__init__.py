"""工具通用机制：执行契约、注册、装饰器和 Schema 转换。

具体工具定义放在父目录 tools/；授权、结果处理及终端实现由各自的同级子包承担。
"""

__all__ = ["BaseTool"]


def __getattr__(name: str):
    """按需保留 BaseTool 导出，单独导入终端 Adapter 时不加载工具契约。

    Args:
        name (str): 请求的公开类型名称。

    Returns:
        type: 既有 BaseTool 类。

    Raises:
        AttributeError: 请求的名称不是公开导出。
    """
    if name == "BaseTool":
        from app.agent.tools.core.base import BaseTool

        return BaseTool
    raise AttributeError(name)
