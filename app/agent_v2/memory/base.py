"""记忆系统基础数据类型定义"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class MemoryType(str, Enum):
    """记忆类型枚举"""
    SUMMARY = "summary"
    EPISODIC = "episodic"
    SEMANTIC = "semantic"


@dataclass
class MemoryItem:
    """记忆项基类"""
    id: str                              # 格式: {type}_{UUID}
    session_id: str                      # 会话ID
    content: str                         # 记忆内容
    timestamp: datetime                  # 事件发生时间（LLM 提取）
    created_at: datetime                 # 记忆创建时间（系统时间）
    importance: float = 0.5              # 重要性 [0, 1]，LLM 评估
    metadata: dict[str, Any] = field(default_factory=dict)  # 扩展元数据


@dataclass
class Entity:
    """实体 - 语义记忆中的知识单元"""
    name: str                            # 实体名称（"用户"、"我"、"张三"等）
    first_seen: datetime | None = None   # 首次出现时间


@dataclass
class Relation:
    """关系 - 实体间的关联"""
    subject: str                         # 主体实体名称
    relation: str                        # 关系（自然语言，如"是"、"喜欢"）
    obj: str                             # 客体实体名称
    time_note: str | None = None         # 时间状语（如"小时候"）
    is_single_value: bool = True         # 是否为单值属性
    memory_id: str | None = None         # 关联的记忆 ID
    session_id: str | None = None        # 会话 ID
    is_current: bool = True              # 是否当前有效
    created_at: datetime | None = None   # 创建时间
