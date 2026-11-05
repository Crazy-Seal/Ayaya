from __future__ import annotations

import asyncio


class SettingsMutationCoordinator:
    """协调配置写入，以及执行受理与删除提交的先后顺序。

    Attributes:
        lock: 串行化关联配置写入，允许配置更新等待连接释放。
        admission_lock: 只串行执行受理与删除提交，持锁期间不清理资源。
    """

    def __init__(self) -> None:
        """创建进程内共享的配置写入锁。"""
        self.lock = asyncio.Lock()
        self.admission_lock = asyncio.Lock()
