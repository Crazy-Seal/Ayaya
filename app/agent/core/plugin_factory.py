"""为独立 Agent 和应用 Agent 统一装配插件的显式运行依赖。"""

from typing import Any

from app.agent.plugins.base import BasePlugin
from app.crud.sqlite_factory import SqliteFactory


def create_plugin(plugin_class: type[BasePlugin], config: dict[str, Any], sqlite_factory: SqliteFactory) -> BasePlugin:
    """使用当前 Agent 借用的连接配置创建插件，内置记忆工具随插件一起装配。

    Args:
        plugin_class (type[BasePlugin]): 注册表已经校验的插件类型。
        config (dict[str, Any]): 当前会话保存的插件配置。
        sqlite_factory (SqliteFactory): 应用或独立调用者提供的连接工厂。

    Returns:
        BasePlugin: 已绑定所需持久化依赖的插件实例。
    """
    from app.agent.plugins.memory import MemoryPlugin
    if issubclass(plugin_class, MemoryPlugin):
        return plugin_class(sqlite_factory=sqlite_factory, **config)
    return plugin_class(**config)
