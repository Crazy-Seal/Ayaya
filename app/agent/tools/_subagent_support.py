"""父工具共用的请求回执处理，以及框架注入的子侧通信工具。"""

from __future__ import annotations

import asyncio
import json

from app.agent.runtime.subagent_requests import (
    ParentMessageRequest, SubagentSource,
)
from app.agent.tools.core.base import BaseTool, ToolContext, ToolResult, InterruptEvent
from app.agent.tools.core.runtime import ToolRuntime
from app.schemas.runtime_types import InterruptKind, CommandReceipt
from dataclasses import asdict
from app.agent.state.manager import PARENT_MESSAGE_DELIVERED


def _runtime(context: ToolContext) -> ToolRuntime:
    """取得框架固定的调用来源，独立未装配工具不能伪造来源执行。"""
    if context.runtime is None:
        raise RuntimeError("当前 Agent 未装配后台执行能力")
    return context.runtime


def _source(runtime: ToolRuntime) -> SubagentSource:
    """从运行时建立来源，父 ID 和执行编号不由模型填写。"""
    return SubagentSource(runtime.session_id, runtime.run_id, runtime.tool_call_id, runtime.subagent_id)


def _receipt_result(receipt: CommandReceipt) -> ToolResult:
    """把持久受理时的回执转换成工具结果，后续执行结果通过父 Inbox 交付。

    Args:
        receipt (CommandReceipt): Scheduler 已完成持久受理的业务回执。

    Returns:
        ToolResult: 包含身份、受理动作及受理时状态的 JSON 正文。
    """
    data = {**asdict(receipt), "accepted": receipt.accepted}
    return ToolResult.success(json.dumps(data, ensure_ascii=False))


class MessageParentTool(BaseTool):
    """子发送问题时返回暂停材料，父指令通过普通引导继续执行。"""
    name = "message_parent"
    description = "给父 Agent 发送明确消息；expect_reply=true 时暂停等待父的新指令，无自动超时。父指令通过普通引导交付。"
    parameters_schema = {"type": "object", "properties": {
        "text": {"type": "string"}, "expect_reply": {"type": "boolean", "default": True},
    }, "required": ["text"], "additionalProperties": False}

    async def execute(self, args: dict, context: ToolContext) -> ToolResult:
        """准备父消息中断或投递普通消息，持久化成功后才确认发送。

        Args:
            args (dict): 子 Agent 的消息正文及是否等待父回复。
            context (ToolContext): 提供框架绑定的父子身份及共享请求队列。

        Returns:
            ToolResult: 待原子保存的父消息中断，或普通投递成功提示。
        """
        runtime = _runtime(context)
        if runtime.subagent_id is None:
            raise ValueError("只有子 Agent 可以向父发送消息")
        text = args.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("消息正文不能为空")
        request_id = f"{runtime.run_id}:{runtime.tool_call_id}"
        if bool(args.get("expect_reply", True)):
            return ToolResult(content=(), interrupt=InterruptEvent(
                InterruptKind.PARENT_MESSAGE, request_id, text,
                {"parent_session_id": runtime.session_id, "subagent_id": runtime.subagent_id, "text": text}))
        reply = asyncio.get_running_loop().create_future()
        await runtime.runtime_queue.put(ParentMessageRequest(_source(runtime), request_id, text, reply))
        await reply
        return ToolResult.success(PARENT_MESSAGE_DELIVERED)
