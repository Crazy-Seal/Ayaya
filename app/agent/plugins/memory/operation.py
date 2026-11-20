"""为单次记忆检索或后台任务打开资源，退出时完成清理。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from weakref import WeakKeyDictionary

from app.agent.plugins.memory.config import MemoryConfig, MemoryPluginConfig
from app.agent.plugins.memory.manager import MemoryManager
from app.agent.utils.infra.owned_work import complete_owned_work
from app.crud.sqlite_factory import SqliteFactory
from app.schemas.chat_settings import ChatSettings


_directory_locks: WeakKeyDictionary[asyncio.AbstractEventLoop, dict[Path, asyncio.Lock]] = WeakKeyDictionary()


def semantic_directory_lock(path: str) -> asyncio.Lock:
    """让同一事件循环内的语义操作按目录排队，避免本地存储锁冲突。

    Args:
        path (str): 本次客户端使用的向量目录。

    Returns:
        asyncio.Lock: 在资源打开至实际关闭期间持有的目录锁。
    """
    locks = _directory_locks.setdefault(asyncio.get_running_loop(), {})
    return locks.setdefault(Path(path).resolve(), asyncio.Lock())


@dataclass(frozen=True)
class MemoryOperation:
    """携带提交时的配置材料，供跨轮次任务自行创建所需资源。

    Attributes:
        settings (ChatSettings): 当前执行的独立模型和角色配置快照。
        config (MemoryConfig): 提交时解析的环境和存储配置。
        options (MemoryPluginConfig): 当前记忆能力开关。
        sqlite_factory (SqliteFactory): 创建操作内连接的通用配置。
    """

    settings: ChatSettings
    config: MemoryConfig
    options: MemoryPluginConfig
    sqlite_factory: SqliteFactory

    @asynccontextmanager
    async def open(self, session_id: str, *, diary: bool = False, episodic: bool = False,
                   semantic: bool = False) -> AsyncIterator[MemoryManager]:
        """只装配本次需要的能力，关闭全部资源后再释放语义目录锁。

        Args:
            session_id (str): 本次记忆读取或写入所属的实际会话。
            diary (bool): 本次是否需要日记存储。
            episodic (bool): 本次是否需要情景记忆。
            semantic (bool): 本次是否需要语义记忆。

        Yields:
            MemoryManager: 仅在当前操作范围内有效的业务对象。
        """
        options = self.options.model_copy(update={
            "enable_diary": diary and self.options.enable_diary,
            "enable_episodic": episodic and self.options.enable_episodic,
            "enable_semantic": semantic and self.options.enable_semantic,
        })
        lock = semantic_directory_lock(self.config.mem0_qdrant_path) if options.enable_semantic else None
        if lock is not None:
            await lock.acquire()
        try:
            manager = MemoryManager(session_id, self.config, self.settings, options=options,
                                    sqlite_factory=self.sqlite_factory)
            try:
                yield manager
            finally:
                await complete_owned_work(manager.close())
        finally:
            if lock is not None:
                lock.release()
