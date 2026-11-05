"""保存同一父会话的真人审批展示顺序。"""

from collections import defaultdict, deque

class ApprovalQueue:
    """只保存同一父会话的展示顺序，决定和审批材料仍属于各上下文。"""

    def __init__(self) -> None:
        """建立父会话到实际上下文、请求编号的 FIFO。"""
        self._pending: dict[str, deque[tuple[str, str]]] = defaultdict(deque)

    def enqueue(self, session_id: str, context_id: str, request_id: str) -> None:
        """将尚未排队的有效审批加入末尾。"""
        entry = (context_id, request_id)
        if entry not in self._pending[session_id]:
            self._pending[session_id].append(entry)

    def head(self, session_id: str) -> tuple[str, str] | None:
        """返回应向用户展示的实际上下文与请求编号。"""
        entries = self._pending.get(session_id)
        return entries[0] if entries else None

    def remove(self, session_id: str, context_id: str, request_id: str | None = None) -> None:
        """停止、引导或决定生效后移除匹配请求，保留其余请求的先后顺序。"""
        entries = self._pending.get(session_id)
        if entries is not None:
            self._pending[session_id] = deque(entry for entry in entries
                                            if entry[0] != context_id or (request_id is not None and entry[1] != request_id))
