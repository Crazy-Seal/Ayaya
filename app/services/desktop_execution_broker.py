"""与聊天事件独立的 Electron 主进程执行请求端口。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable

from app.schemas.runtime_types import (
    DesktopExecutionKind, InterruptKind,
)
from app.agent.agent import Agent
from app.agent.state.manager import StateManager


DESKTOP_EXECUTION_QUEUE_MIN_SIZE = 64


@dataclass(frozen=True, slots=True)
class DesktopExecutionRequest:
    """描述一次主进程屏幕动作，request_id 与持久 continuation 一一对应。

    Attributes:
        session_id (str): 共享 Ayaya 会话标识。
        request_id (str): checkpoint 中的持久中断标识。
        kind (DesktopExecutionKind | str): 已知执行类别，未知请求仍沿原领取及错误路径处理。
        payload (dict[str, Any]): 该类别携带的专用载荷。
    """
    session_id: str
    request_id: str
    kind: DesktopExecutionKind | str
    payload: dict[str, Any]

    def __post_init__(self) -> None:
        """归一化已知执行类别，未知值仍由原有执行器返回错误。
        """
        try:
            object.__setattr__(self, "kind", DesktopExecutionKind(self.kind))
        except (ValueError, TypeError):
            pass


class DesktopExecutionBroker:
    """管理所有会话的截图和桌面操作请求，交给 Electron 主进程执行。

    Scheduler 共用一个 Broker，通过 session_id 和 request_id 区分请求。
    Electron 必须先领取请求，后端保存领取标记后才允许操作屏幕，避免重连重复点击。

    Attributes:
        _state_manager_factory (Callable[[str], StateManager]): 按会话加载持久状态的工厂。
        _agent_factory (Callable[[str], Agent]): 按会话创建短生命周期 Agent 的工厂。
        _on_result (Callable[[str, str, dict], Any]): 持久结果就绪后触发运行恢复的回调。
        _pending (dict[tuple[str, str], DesktopExecutionRequest]): 尚未成功领取的屏幕请求，按会话和请求标识索引。
        _subscribers (set[asyncio.Queue[DesktopExecutionRequest | None]]): 接收增量的有界订阅队列集合。
        _pending_lock (asyncio.Lock): 屏幕入队与订阅快照的共同屏障。
        _locks (dict[str, asyncio.Lock]): 按会话序列化发布或执行状态迁移的短锁。
        _accepting (bool): 当前生命周期是否允许请求、领取和结果受理。
    """

    def __init__(
        self,
        *,
        state_manager_factory: Callable[[str], StateManager],
        agent_factory: Callable[[str], Agent],
        on_result: Callable[[str, str, dict], Any],
    ) -> None:
        """注入状态和授权复核工厂，以及交付结果后的恢复回调。

        Args:
            state_manager_factory (Callable[[str], StateManager]): 按会话加载持久状态的工厂。
            agent_factory (Callable[[str], Agent]): 按会话创建短生命周期 Agent 的工厂。
            on_result (Callable[[str, str, dict], Any]): 持久结果就绪后触发运行恢复的回调。
        """
        self._state_manager_factory = state_manager_factory
        self._agent_factory = agent_factory
        self._on_result = on_result
        self._pending: dict[tuple[str, str], DesktopExecutionRequest] = {}
        self._subscribers: set[asyncio.Queue[DesktopExecutionRequest | None]] = set()
        self._pending_lock = asyncio.Lock()
        self._locks: dict[str, asyncio.Lock] = {}
        self._accepting = True

    def start(self) -> None:
        """持久清理完成后开启本次进程的桌面执行入口。"""
        self._accepting = True

    def begin_shutdown(self) -> None:
        """立即拒绝新操作、撤销未领取请求并结束执行流，不等待持久写入。"""
        self._accepting = False
        self._pending.clear()
        for queue in tuple(self._subscribers):
            while not queue.empty():
                queue.get_nowait()
            queue.put_nowait(None)
        self._subscribers.clear()

    async def close(self) -> None:
        """封住入口后等待已登记的会话持久操作退出，供旧轮次安全收尾。"""
        self.begin_shutdown()
        # 1. 新操作在获取锁后再次检查入口，只有此前已进入的写入需要等待。
        for lock in tuple(self._locks.values()):
            async with lock:
                pass
        async with self._pending_lock:
            pass

    async def enqueue(self, request: DesktopExecutionRequest) -> None:
        """在订阅屏障内登记待执行请求，慢执行器断开后可从 pending 重连。

        Args:
            request (DesktopExecutionRequest): 待执行桌面动作或 HTTP 请求模型。
        """
        key = (request.session_id, request.request_id)
        # 1. 入队与订阅快照共用屏障，确保新请求不会落在登记订阅的空隙中。
        async with self._pending_lock:
            if not self._accepting:
                return
            self._pending[key] = request
            for queue in tuple(self._subscribers):
                if not queue.full():
                    queue.put_nowait(request)
                # 2. 慢执行器队列满时断开，仍未领取请求保留在 pending 供重连恢复。
                else:
                    self._subscribers.discard(queue)
                    while not queue.empty():
                        queue.get_nowait()
                    queue.put_nowait(None)

    async def subscribe(self) -> AsyncIterator[DesktopExecutionRequest]:
        """先提供未领取请求，再持续提供增量，订阅结束不改变工具状态。

        Yields:
            DesktopExecutionRequest: 仍待领取的屏幕请求，包含现存请求及新增请求。
        """
        queue: asyncio.Queue[DesktopExecutionRequest | None]
        # 1. 先把现存未领取请求放入队列，再登记增量订阅。
        async with self._pending_lock:
            if not self._accepting:
                return
            queue = asyncio.Queue(maxsize=max(DESKTOP_EXECUTION_QUEUE_MIN_SIZE, len(self._pending) + 1))
            for request in self._pending.values():
                queue.put_nowait(request)
            self._subscribers.add(queue)
        try:
            while True:
                request = await queue.get()
                if request is None:
                    return
                yield request
        # 2. 连接结束只清理订阅者，工具请求由持久 checkpoint 决定是否有效。
        finally:
            self._subscribers.discard(queue)

    async def claim(self, session_id: str, request_id: str) -> bool:
        """复核当前请求及单次授权，持久标记领取后才允许桌面执行副作用。

        Args:
            session_id (str): 共享 Ayaya 会话标识。
            request_id (str): checkpoint 中的持久中断标识。

        Returns:
            bool: 本次请求首次领取成功且授权仍有效时为真。
        """
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        rejected_result: dict | None = None
        # 1. 按会话串行核对持久 continuation 和领取标记，重复 claim 直接拒绝。
        async with lock:
            if not self._accepting:
                return False
            manager = self._state_manager_factory(session_id)
            agent: Agent | None = None
            try:
                state = await manager.load()
                interrupt = state.interrupt_data
                if (not interrupt or interrupt.get("type") != InterruptKind.CONTINUATION_REQUEST
                        or interrupt.get("request_id") != request_id
                        or (interrupt.get("resume_state") or {}).get("continuation_claimed")):
                    return False
                agent = self._agent_factory(session_id)
                # 2. 执行前复核单次授权，撤销授权时保存拒绝结果而不执行动作。
                if not await agent.validate_pending_continuation({
                    **interrupt, "session_id": session_id,
                }):
                    rejected_result = {"executed": False, "error": "工具授权已失效"}
                if not self._accepting:
                    return False
                resume_state = {
                    **(interrupt.get("resume_state") or {}),
                    "continuation_claimed": True,
                }
                updated = {**interrupt, "resume_state": resume_state}
                if rejected_result is not None:
                    updated["desktop_result"] = rejected_result
                state.set_interrupt(updated, state.pending_actions)
                # 3. 先持久写入 continuation_claimed 再返回成功，屏幕副作用只能发生在这之后。
                await manager.save(state, checkpoint_type="intermediate")
                async with self._pending_lock:
                    self._pending.pop((session_id, request_id), None)
            finally:
                await manager.close()
                if agent is not None:
                    await agent.close()
        # 4. 授权拒绝也触发后端恢复，避免轮次永远停在执行等待。
        if rejected_result is not None:
            callback_result = self._on_result(session_id, request_id, rejected_result)
            if asyncio.iscoroutine(callback_result):
                await callback_result
            return False
        # 5. 领取保存期间若进入关闭，不能再向执行器发放动作许可；持久领取按未知结果收尾。
        return self._accepting

    async def submit_result(self, session_id: str, request_id: str, result: dict) -> bool:
        """仅持久接受尚无结果的已领取请求，再通知 Scheduler 继续工具链。

        Args:
            session_id (str): 共享 Ayaya 会话标识。
            request_id (str): checkpoint 中的持久中断标识。
            result (dict): 桌面执行结果或任务终态输出摘要。

        Returns:
            bool: 首个有效执行结果已经持久接受时为真。
        """
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        # 1. 请求必须匹配、已领取且尚无结果，迟到或重复结果不能恢复另一工具。
        async with lock:
            if not self._accepting:
                return False
            manager = self._state_manager_factory(session_id)
            try:
                state = await manager.load()
                interrupt = state.interrupt_data
                if not self._accepting:
                    return False
                if (not interrupt or interrupt.get("type") != InterruptKind.CONTINUATION_REQUEST
                        or interrupt.get("request_id") != request_id
                        or not (interrupt.get("resume_state") or {}).get("continuation_claimed")
                        or "desktop_result" in interrupt):
                    return False
                # 2. 先保存真实结果再通知本次运行续跑；重启只归档事实并终止旧轮次。
                state.set_interrupt({**interrupt, "desktop_result": result}, state.pending_actions)
                await manager.save(state, checkpoint_type="intermediate")
            finally:
                await manager.close()
        callback_result = self._on_result(session_id, request_id, result)
        if asyncio.iscoroutine(callback_result):
            await callback_result
        return True
