"""
插件基类

定义插件的基本接口。
"""

# 重新导出 context.py 中的 BasePlugin
from app.agent.context import BasePlugin, PluginHook

__all__ = ["BasePlugin", "PluginHook"]
