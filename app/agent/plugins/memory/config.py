"""
记忆系统配置
"""

import os
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from pydantic import BaseModel, ConfigDict, Field

from app.environment import get_data_dir, require_test_storage_path


class MemoryPluginConfig(BaseModel):
    """声明记忆能力的默认开关和真人输入提取间隔，供设置页和执行共用。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    enable_diary: bool = Field(default=True, title="日记记忆", description="是否启用日记记忆。")
    enable_episodic: bool = Field(default=True, title="情景记忆", description="是否启用情景记忆。")
    enable_semantic: bool = Field(default=True, title="语义记忆", description="是否启用语义记忆。")
    long_term_memory_every_human_messages: int = Field(
        default=10, ge=1, title="长期记忆提取间隔",
        description="每多少轮真实用户消息触发长期记忆总结/抽取。",
    )


def get_memory_base_dir() -> Path:
    """解析记忆数据目录，并沿用应用提供的测试存储隔离检查。

    Returns:
        Path: 当前记忆存储的绝对目录。
    """
    configured = os.getenv("MEMORY_BASE_PATH", "").strip()
    return require_test_storage_path(Path(configured) if configured else get_data_dir(), "MEMORY_BASE_PATH")


def get_mem0_qdrant_dir(memory_base_dir: Path) -> Path:
    """解析语义向量目录，并检查测试操作始终位于隔离目录。

    Args:
        memory_base_dir (Path): 本次记忆存储的根目录。

    Returns:
        Path: 本次语义向量存储的绝对目录。
    """
    configured = os.getenv("MEM0_QDRANT_PATH", "").strip()
    return require_test_storage_path(Path(configured) if configured else memory_base_dir / "mem0" / "qdrant_data", "MEM0_QDRANT_PATH")


def get_system_timezone() -> ZoneInfo:
    """读取本机时区，供日记有效日期和记忆时间展示使用。"""
    return datetime.now().astimezone().tzinfo


@dataclass(frozen=True)
class MemoryConfig:
    """集中保存记忆提取模型、向量模型、隔离存储路径、时区及提取预算。"""
    # 存储路径
    sqlite_path: str = ""
    chroma_path: str = ""

    # Mem0 配置
    mem0_qdrant_path: str = ""
    mem0_collection_name: str = "Ayaya_semantic_memory"

    # 嵌入配置
    embedding_api_key: str = ""
    embedding_model: str = ""
    embedding_dimension: int = 1024
    embedding_base_url: str = ""

    # 情景记忆提取模型
    episodic_extraction_model: str = ""
    episodic_extraction_api_key: str = ""
    episodic_extraction_base_url: str = ""
    extraction_max_context_tokens: int | None = None
    semantic_extraction_model: str = "gpt-4o"
    semantic_extraction_api_key: str = ""
    semantic_extraction_base_url: str = ""

    # 记忆参数
    recent_diaries_count: int = 2        # 获取最近 N 天日记
    episodic_top_k: int = 3              # 情景记忆检索数量
    semantic_top_k: int = 3              # 语义记忆检索数量

    # 时区配置
    timezone: ZoneInfo = field(default_factory=get_system_timezone)
    day_boundary_hour: int = 4           # 新的一天分界点（小时）

    @classmethod
    def from_env(cls) -> "MemoryConfig":
        """从当前环境读取记忆模型和存储配置，所有持久路径经运行环境隔离规则解析。"""
        base_path = get_memory_base_dir()

        tz_name = os.getenv("MEMORY_TIMEZONE")
        if tz_name:
            timezone = ZoneInfo(tz_name)
        else:
            timezone = get_system_timezone()

        day_boundary_hour = int(os.getenv("MEMORY_DAY_BOUNDARY_HOUR", "4"))

        return cls(
            sqlite_path=str(base_path / "sqlite" / "memory.sqlite3"),
            chroma_path=str(base_path / "chroma"),
            mem0_qdrant_path=str(get_mem0_qdrant_dir(base_path)),
            mem0_collection_name=os.getenv("MEM0_COLLECTION_NAME", "Ayaya_semantic_memory"),
            embedding_api_key=os.getenv("EMBEDDING_API_KEY", ""),
            embedding_model=os.getenv("EMBEDDING_MODEL", ""),
            embedding_dimension=int(os.getenv("EMBEDDING_DIMENSION", "1024")),
            embedding_base_url=os.getenv("EMBEDDING_BASE_URL", ""),
            episodic_extraction_model=os.getenv("EPISODIC_EXTRACTION_MODEL", ""),
            episodic_extraction_api_key=os.getenv("EPISODIC_EXTRACTION_API_KEY", ""),
            episodic_extraction_base_url=os.getenv("EPISODIC_EXTRACTION_BASE_URL", ""),
            semantic_extraction_model=os.getenv("MEM0_EXTRACTION_MODEL", "gpt-4o"),
            semantic_extraction_api_key=os.getenv("MEM0_EXTRACTION_API_KEY", ""),
            semantic_extraction_base_url=os.getenv("MEM0_EXTRACTION_BASE_URL", ""),
            extraction_max_context_tokens=(
                int(os.environ["MEMORY_EXTRACTION_MAX_CONTEXT_TOKENS"])
                if os.getenv("MEMORY_EXTRACTION_MAX_CONTEXT_TOKENS")
                else None
            ),
            timezone=timezone,
            day_boundary_hour=day_boundary_hour,
        )
