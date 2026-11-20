"""记忆系统基础数据类型定义"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


class MemoryExtractionError(RuntimeError):
    """报告提取或存储失败，同时保留本次已经保存的记忆数量。

    Attributes:
        saved_count (int): 本次已经成功保存的记忆数量，包含出错后继续处理成功的条目。
    """

    def __init__(self, message: str, *, saved_count: int = 0) -> None:
        """保存错误摘要和已完成数量，供上层汇总任务失败原因。

        Args:
            message (str): 本次提取或存储失败的原因。
            saved_count (int): 本次已经保存的记忆条数。
        """
        super().__init__(message)
        self.saved_count = saved_count


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
