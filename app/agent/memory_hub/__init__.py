from app.agent.memory_hub.config import MemoryConfig, memory_config_from_chat_settings
from app.agent.memory_hub.manager import MemoryManager, build_default_memory_manager
from app.agent.memory_hub.storage.adapters import get_short_term_adapter, get_vector_memory_adapter
from app.agent.memory_hub.text_utils import get_last_human_text


def get_vector_memory_adapter_instance():
    return get_vector_memory_adapter()

__all__ = [
    "MemoryManager",
    "build_default_memory_manager",
    "MemoryConfig",
    "memory_config_from_chat_settings",
    "get_vector_memory_adapter_instance",
    "get_last_human_text",
]
