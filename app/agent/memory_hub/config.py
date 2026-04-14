from pydantic import BaseModel

from app.schemas.chat_settings import ChatSettings


class MemoryConfig(BaseModel):
    """记忆模块配置，仅包含记忆子系统所需参数。"""

    # 记忆抽取模型配置
    model_name: str
    openai_api_key: str
    openai_base_url: str

    # 可选：启用插件列表；为空时使用默认插件。
    memory_plugins: list[str] | None = None

    # 摘要记忆参数（替代工作记忆配置）
    short_term_min_chars: int = 50
    short_term_max_chars: int = 300

    def __hash__(self) -> int:
        return hash(
            (
                self.model_name,
                self.openai_api_key,
                self.openai_base_url,
                tuple(self.memory_plugins or []),
                self.short_term_min_chars,
                self.short_term_max_chars,
            )
        )


def memory_config_from_chat_settings(chat_settings: ChatSettings) -> MemoryConfig:
    """边界层适配：把 ChatSettings 映射为 MemoryConfig。"""
    return MemoryConfig(
        model_name=chat_settings.model_name,
        openai_api_key=chat_settings.openai_api_key,
        openai_base_url=chat_settings.openai_base_url,
        memory_plugins=chat_settings.memory_plugins,
    )

