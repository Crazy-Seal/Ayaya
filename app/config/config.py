import yaml
from functools import lru_cache
from pathlib import Path
from typing import Any
from app.schemas.chat_settings import ChatSettings

CHAT_CONFIG_FILE = Path(__file__).resolve().parents[2] / "config" / "chat_settings.yaml"

def _load_yaml_with_env(file_path: Path) -> dict[str, Any]:
    with file_path.open("r", encoding="utf-8") as file:
        raw = yaml.safe_load(file) or {}
    return raw


@lru_cache
def get_chat_settings(session_id: str) -> ChatSettings:
    # 从 YAML 文件读取配置
    if not CHAT_CONFIG_FILE.exists():
        raise RuntimeError(f"Config file not found: {CHAT_CONFIG_FILE}")

    raw = _load_yaml_with_env(CHAT_CONFIG_FILE)

    chat_models = raw["chat_models"]
    matched_model = next(model for model in chat_models if model["session_id"] == session_id)

    return ChatSettings(
        session_id=matched_model["session_id"],
        model_name=matched_model["model_name"],
        openai_api_key=matched_model["openai_api_key"],
        openai_base_url=matched_model["openai_base_url"],
        temperature=matched_model["temperature"],
        system_prompt=matched_model["system_prompt"],
        tools_list=matched_model["tools_list"],
        memory_plugins=matched_model.get("memory_plugins"),
        # 提示词模板字段
        name=matched_model.get("name"),
        feature=matched_model.get("feature"),
        character=matched_model.get("character"),
        address=matched_model.get("address"),
        characteristic=matched_model.get("characteristic"),
        constraint=matched_model.get("constraint"),
    )
