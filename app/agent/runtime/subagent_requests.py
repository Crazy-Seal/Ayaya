"""父子 Agent 的进程内专用请求和回执，不引用执行管理对象。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from app.schemas.subagent_types import SubagentAction, SubagentSummary
from app.schemas.runtime_types import CommandReceipt
from app.schemas.tool_identity import ToolIdentity

if TYPE_CHECKING:
    from app.agent.runtime.requests import RuntimeRequest


@dataclass(frozen=True)
class SubagentSource:
    """由框架绑定的调用来源，模型只填写业务参数。"""
    session_id: str
    run_id: str
    tool_call_id: str
    subagent_id: str | None = None




@dataclass(frozen=True)
class CreateSubagentRequest:
    """父工具要求保存定义和初始任务，返回新身份及持久受理回执。"""
    source: SubagentSource
    name: str
    system_prompt: str
    prompt: str
    tool_whitelist: frozenset[ToolIdentity]
    if_retain_context: bool
    reply: asyncio.Future[CommandReceipt] = field(repr=False)


@dataclass(frozen=True)
class InteractSubagentRequest:
    """按受理时实际状态路由命令、停止或销毁。"""
    source: SubagentSource
    subagent_id: str
    action: SubagentAction
    text: str | None
    reply: asyncio.Future[CommandReceipt] = field(repr=False)


@dataclass(frozen=True)
class SubagentDetailRequest:
    """按父子归属读取单体详情，不支持省略编号查询全部。"""
    source: SubagentSource
    subagent_id: str
    interaction_limit: int
    reply: asyncio.Future[dict[str, object]] = field(repr=False)


@dataclass(frozen=True)
class SubagentSnapshotRequest:
    """框架在请求模型前查询父会话当前名单，不是模型工具。"""
    session_id: str
    reply: asyncio.Future[tuple[SubagentSummary, ...]] = field(repr=False)


@dataclass(frozen=True)
class ParentMessageRequest:
    """子工具投递无需等待父指令的消息，Future 仅确认保存受理。"""
    source: SubagentSource
    request_id: str
    text: str
    reply: asyncio.Future[None] = field(repr=False)


SubagentRequest = CreateSubagentRequest | InteractSubagentRequest | SubagentDetailRequest | SubagentSnapshotRequest | ParentMessageRequest


async def subagent_environment(queue: "asyncio.Queue[RuntimeRequest]", session_id: str) -> str:
    """通过共享队列查询当前名单，供父模型请求前刷新环境。

    Args:
        queue (asyncio.Queue[RuntimeRequest]): Scheduler 消费的运行时请求队列。
        session_id (str): 需要刷新子 Agent 名单的父会话编号。

    Returns:
        str: 当前名单及运行状态；名单为空时返回说明文字。
    """
    future = asyncio.get_running_loop().create_future()
    await queue.put(SubagentSnapshotRequest(session_id, future))
    summaries = await future
    labels = {"idle": "空闲", "running": "正在运行", "waiting_parent": "等待你的回复"}
    return "\n".join(f"{item.subagent_id} | {item.name} | {'可复用' if item.if_retain_context else '一次性'} | "
                     f"{labels[item.status]}" for item in summaries) or "当前没有子 Agent。"
