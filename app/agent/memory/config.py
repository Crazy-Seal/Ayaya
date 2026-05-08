"""记忆系统配置"""

import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# 项目根目录：app/agent/memory/config.py -> 项目根
PROJECT_ROOT = Path(__file__).resolve().parents[3]


def get_system_timezone() -> ZoneInfo:
    """获取系统时区"""
    # datetime.now().astimezone() 会返回带系统时区的时间
    # .tzinfo 就是系统时区
    return datetime.now().astimezone().tzinfo


@dataclass
class MemoryConfig:
    """记忆系统配置"""
    # 存储路径
    sqlite_path: str = ""
    chroma_path: str = ""
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "neo4j"

    # 语义记忆后端选择
    semantic_backend: str = "mem0"  # "native" | "mem0"

    # Mem0 配置
    mem0_qdrant_path: str = ""
    mem0_collection_name: str = "Ayaya_semantic_memory"

    # 嵌入配置
    embedding_api_key: str = ""
    embedding_model: str = ""
    embedding_dimension: int = 1024
    embedding_base_url: str = ""

    # 记忆参数
    summary_every_messages: int = 10     # 每 N 条消息生成一次摘要
    recent_summaries_count: int = 2      # 获取最近 N 天摘要
    episodic_top_k: int = 3              # 情景记忆检索数量
    semantic_top_k: int = 3              # 语义记忆检索数量

    # 时区配置
    timezone: ZoneInfo = field(default_factory=get_system_timezone)
    day_boundary_hour: int = 4           # 新的一天分界点（小时）

    @classmethod
    def from_env(cls) -> "MemoryConfig":
        """从环境变量加载配置"""
        # 存储路径：默认在项目根目录下的 memory 文件夹
        env_base = os.getenv("MEMORY_BASE_PATH")
        base_path = Path(env_base) if env_base else PROJECT_ROOT / "memory"

        # 时区配置
        tz_name = os.getenv("MEMORY_TIMEZONE")  # 如 "Asia/Shanghai", "America/New_York"
        if tz_name:
            timezone = ZoneInfo(tz_name)
        else:
            timezone = get_system_timezone()

        # 新的一天分界点
        day_boundary_hour = int(os.getenv("MEMORY_DAY_BOUNDARY_HOUR", "4"))

        return cls(
            sqlite_path=str(base_path / "sqlite" / "memory.sqlite3"),
            chroma_path=str(base_path / "chroma"),
            neo4j_uri=os.getenv("NEO4J_URI", "bolt://localhost:7687"),
            neo4j_user=os.getenv("NEO4J_USER", "neo4j"),
            neo4j_password=os.getenv("NEO4J_PASSWORD", ""),
            semantic_backend=os.getenv("SEMANTIC_BACKEND", "mem0"),
            mem0_qdrant_path=os.getenv("MEM0_QDRANT_PATH", str(base_path / "mem0" / "qdrant_data")),
            mem0_collection_name=os.getenv("MEM0_COLLECTION_NAME", "Ayaya_semantic_memory"),
            embedding_api_key=os.getenv("EMBEDDING_API_KEY", ""),
            embedding_model=os.getenv("EMBEDDING_MODEL", ""),
            embedding_dimension=int(os.getenv("EMBEDDING_DIMENSION", "1024")),
            embedding_base_url=os.getenv("EMBEDDING_BASE_URL", ""),
            timezone=timezone,
            day_boundary_hour=day_boundary_hour,
        )
