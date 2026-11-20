from tests.memory_support import operation_for
"""验证日记查询工具按日期范围筛选，并最多显示最早的五篇日记。"""
from app.crud.sqlite_factory import SqliteFactory

from datetime import date
from pathlib import Path
import re

import pytest

from app.agent.plugins.memory import manager as memory_manager_module
from app.agent.plugins.memory.manager import MemoryManager
from app.agent.plugins.memory.memories.diary import DiaryMemory
from app.agent.plugins.memory.store.diary_sqlite_store import DiarySqliteStore
from app.agent.plugins.memory.tools.search_diary import SearchDiaryTool
from app.agent.state import AgentState
from app.agent.tools.core.base import ToolContext


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "start_day,end_day,saved_days,expected_days,hidden_count",
    [
        (1, 31, [20, 3, 10, 6, 25, 15, 30], [3, 6, 10, 15, 20], 2),
        (1, 6, [6, 2, 4, 1, 5, 3], [1, 2, 3, 4, 5], 1),
        (3, 25, [1, 25, 10, 3, 30], [3, 10, 25], 0),
        (1, 31, [25, 1, 20, 5, 12], [1, 5, 12, 20, 25], 0),
        (10, 10, [9, 11, 10], [10], 0),
        (10, 20, [9, 21], [], 0),
        (20, 10, [10, 15, 20], [], 0),
    ],
    ids=["wide-sparse", "six-consecutive", "inclusive-bounds", "exactly-five",
         "single-day", "empty-range", "reversed-range"],
)
async def test_search_diary_returns_first_five_in_date_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    start_day: int,
    end_day: int,
    saved_days: list[int],
    expected_days: list[int],
    hidden_count: int,
) -> None:
    """工具查询真实临时日记库，验证范围、排序、数量限制、剩余篇数及会话隔离。

    Args:
        tmp_path (Path): Pytest 提供的临时数据库目录。
        monkeypatch (pytest.MonkeyPatch): 将工具绑定到本用例的记忆管理器。
        start_day (int): 一月查询开始日。
        end_day (int): 一月查询结束日。
        saved_days (list[int]): 当前会话按此顺序保存日记的日期。
        expected_days (list[int]): 工具应按此顺序显示的日记日期。
        hidden_count (int): 查询范围内超过显示上限的日记篇数。
    """
    store = DiarySqliteStore(str(tmp_path / "diaries.sqlite3"), sqlite_factory=SqliteFactory())
    for day in saved_days:
        await store.add("diary-query", date(2026, 1, day), f"日记正文 {day}")
    await store.add("other-session", date(2026, 1, 1), "其他会话的日记")

    diary = object.__new__(DiaryMemory)
    diary.session_id = "diary-query"
    diary.store = store
    manager = object.__new__(MemoryManager)
    manager.diary_memory = diary

    def get_manager(session_id: str) -> MemoryManager:
        """按工具绑定的会话返回使用临时日记库的管理器。

        Args:
            session_id (str): 工具上下文提供的当前会话编号。

        Returns:
            MemoryManager: 本用例的日记查询管理器。
        """
        assert session_id == diary.session_id
        return manager

    result = await SearchDiaryTool(operation_for(manager)).execute(
        {
            "start": date(2026, 1, start_day).isoformat(),
            "end": date(2026, 1, end_day).isoformat(),
        },
        ToolContext(
            session_id=diary.session_id,
            state=AgentState.create_new(diary.session_id),
        ),
    )

    # 1. 从本用例写入的正文标记识别返回日记，验证筛选、排序和五篇上限。
    visible_days = [int(day) for day in re.findall(r"日记正文 (\d+)", result.text)]
    assert visible_days == expected_days
    assert "其他会话的日记" not in result.text

    # 2. 排除日期和正文中的数字，验证提示包含正确的剩余篇数。
    summary = re.sub(r"\d{4}-\d{2}-\d{2}|日记正文 \d+", "", result.text)
    reported_counts = [int(count) for count in re.findall(r"\d+", summary)]
    assert reported_counts == ([hidden_count] if hidden_count else [])
    assert result.text.strip()
