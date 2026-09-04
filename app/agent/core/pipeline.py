"""
执行管道

负责编排 LLM 调用和工具执行循环。

中断/恢复采用「可持久化」模型：被中断工具的全部路由信息（tool_name/
tool_call_id/tool_args/resume_state）写入 state.interrupt_data，使恢复能
在另一个请求（甚至进程重启后）重入同一工具——无需依赖存活的协程。
"""

import logging
from typing import Any, AsyncIterator, Awaitable, Callable, Protocol, TYPE_CHECKING

from app.agent.context import (
    ToolContext,
    ToolResult,
    PluginHook,
)
from app.agent.core.event_router import EventType, AgentEvent
from app.agent.core.plugin_manager import PluginManager
from app.agent.core.tool_manager import ToolManager
from app.agent.context_strategy import ContextStrategyManager
from app.agent.message import (
    SCREENSHOT_MESSAGE_NAME,
    ToolCall,
    MessageRole,
    AssistantMessageWithTools,
    is_real_human_message,
)
from app.agent.state import AgentState
from app.agent.models.llm_client import StreamChunk
from app.agent.message_time import RuntimeContext
from app.agent.core.state_manager import CheckpointType
from app.agent.tool_result_normalizer import ToolResultNormalizer
from app.agent.model_input import DiaryCoverage, ModelInputCoordinator

if TYPE_CHECKING:
    from app.schemas.chat_settings import ChatSettings

logger = logging.getLogger(__name__)


class CheckpointCallback(Protocol):
    """持久化回调必须显式声明 checkpoint 类型。"""

    async def __call__(
        self,
        state: AgentState,
        *,
        checkpoint_type: CheckpointType,
    ) -> int: ...


class SystemPromptSource(Protocol):
    """Pipeline 构建模型消息所需的最小配置接口。"""

    system_prompt: str
    model_name: str


class StreamingLLMClient(Protocol):
    """Pipeline 调用流式模型所需的最小接口。"""

    def astream(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
    ) -> AsyncIterator[StreamChunk]: ...


EventEmitter = Callable[[EventType, Any], Awaitable[None]]


class ExecutionPipeline:
    """执行管道 - 编排 LLM 调用和工具执行"""

    def __init__(
        self,
        *,
        prompt_source: SystemPromptSource,
        llm_client: StreamingLLMClient,
        tool_manager: ToolManager,
        plugin_manager: PluginManager,
        context_strategy: ContextStrategyManager,
        emit_event: EventEmitter,
        diary_coverage: DiaryCoverage | None = None,
        chat_settings: "ChatSettings | None" = None,
    ) -> None:
        self._prompt_source = prompt_source
        self._llm_client = llm_client
        self._tool_manager = tool_manager
        self._plugin_manager = plugin_manager
        self._context_strategy = context_strategy
        self._emit_event = emit_event
        self._tool_result_normalizer = ToolResultNormalizer()
        self._model_input = ModelInputCoordinator(
            context_strategy,
            llm_client,
            diary_coverage=diary_coverage,
            chat_settings=chat_settings,
        )

    # ==================== 主入口 ====================

    async def execute(
        self,
        state: AgentState,
        checkpoint: CheckpointCallback | None = None,
        runtime_context: RuntimeContext | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """执行主循环（一轮对话的起点）"""
        runtime_context = runtime_context or RuntimeContext.capture()
        latest_human = next(
            (message for message in reversed(state.messages) if is_real_human_message(message)),
            None,
        )
        if latest_human:
            runtime_context.latest_human_message_id = str(latest_human.get("_message_id") or "")
        # ON_INVOKE 钩子 - 每轮仅在此触发一次
        state = await self._plugin_manager.run_hooks(
            PluginHook.ON_INVOKE, state, data={"runtime_context": runtime_context}
        )
        async for event in self._run_loop(
            state, checkpoint, runtime_context
        ):
            yield event

    async def resume_tools(
        self,
        state: AgentState,
        resume_data: dict,
        checkpoint: CheckpointCallback | None = None,
        runtime_context: RuntimeContext | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """从中断恢复：先跑完被中断的工具与剩余待执行工具，再回到 LLM 循环。

        注意：这里不再触发 ON_INVOKE（它属于「新一轮」语义），避免重复副作用。
        """
        interrupt_data = state.interrupt_data or {}
        pending_actions = list(state.pending_actions)
        state.clear_interrupt()

        resumed_calls: list[ToolCall] = []
        tool_name = interrupt_data.get("tool_name")
        if tool_name:
            resumed_calls.append(ToolCall(
                id=interrupt_data.get("tool_call_id", ""),
                name=tool_name,
                args=interrupt_data.get("tool_args", {}),
            ))
        resumed_calls.extend(ToolCall.from_dict(item) for item in pending_actions)
        state.set_pending_tool_calls(resumed_calls)

        # 1. 重入被中断的工具（用持久化的路由信息重建 ToolCall）
        if tool_name:
            interrupted_call = resumed_calls[0]
            result = await self._execute_tool(
                state,
                interrupted_call,
                resume_data=resume_data,
                resume_state=interrupt_data.get("resume_state", {}),
            )

            # 恢复执行时又触发了新的中断
            if result.interrupt:
                self._persist_interrupt(state, interrupted_call, result, pending_actions)
                await self._checkpoint(state, checkpoint)
                yield AgentEvent(EventType.INTERRUPT, result.interrupt.to_client())
                return

            await self._commit_completed_tool_call(
                state,
                interrupted_call,
                result,
                checkpoint,
            )

        # 2. 执行中断时尚未轮到的剩余工具
        for i, tc_dict in enumerate(pending_actions):
            tool_call = ToolCall.from_dict(tc_dict)
            yield AgentEvent(EventType.TOOL_CALL, tool_call.name)

            result = await self._execute_tool(state, tool_call)
            if result.interrupt:
                self._persist_interrupt(state, tool_call, result, pending_actions[i + 1:])
                await self._checkpoint(state, checkpoint)
                yield AgentEvent(EventType.INTERRUPT, result.interrupt.to_client())
                return

            await self._commit_completed_tool_call(
                state,
                tool_call,
                result,
                checkpoint,
            )

        # 3. 回到 LLM 循环（不重跑 ON_INVOKE）
        runtime_context = runtime_context or RuntimeContext.capture()
        async for event in self._run_loop(
            state, checkpoint, runtime_context
        ):
            yield event

    # ==================== 核心循环 ====================

    async def _run_loop(
        self,
        state: AgentState,
        checkpoint: CheckpointCallback | None = None,
        runtime_context: RuntimeContext | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """LLM ↔ 工具循环主体（不含 ON_INVOKE）"""
        while True:
            # 1. BEFORE_LLM 钩子
            state = await self._plugin_manager.run_hooks(
                PluginHook.BEFORE_LLM, state
            )

            # 2. 调用 LLM（流式）
            accumulated_content = ""
            tool_calls: list[ToolCall] = []
            finish_reason: str | None = None

            try:
                runtime_context = runtime_context or RuntimeContext.capture()
                async for chunk in self._call_llm(state, runtime_context, checkpoint):
                    if chunk.usage and chunk.usage.prompt_tokens:
                        self._model_input.calibrate(chunk.usage.prompt_tokens)
                    if chunk.content:
                        accumulated_content += chunk.content
                        yield AgentEvent(EventType.TEXT_CHUNK, chunk.content)
                    if chunk.tool_call:
                        tool_calls.append(chunk.tool_call)
                    if chunk.finish_reason:
                        finish_reason = chunk.finish_reason

                if not tool_calls and not accumulated_content.strip():
                    reason = finish_reason or "unknown"
                    raise RuntimeError(f"模型未返回有效文本（finish_reason={reason}）")
            except Exception as e:
                logger.error("LLM 调用失败: %s", e)
                await self._plugin_manager.run_hooks(
                    PluginHook.ON_ERROR, state, data={"error": e}
                )
                yield AgentEvent(EventType.ERROR, str(e))
                return

            # 3. AFTER_LLM 钩子
            state = await self._plugin_manager.run_hooks(
                PluginHook.AFTER_LLM, state,
                data={"content": accumulated_content, "tool_calls": tool_calls},
            )

            # 4. 助手消息入状态
            if accumulated_content or tool_calls:
                self._add_assistant_message(state, accumulated_content, tool_calls)

            if tool_calls:
                state.set_pending_tool_calls(tool_calls)
                await self._checkpoint(state, checkpoint)

            # 5. 无工具调用 → 本轮结束
            if not tool_calls:
                break

            # 6. 工具循环
            for i, tool_call in enumerate(tool_calls):
                yield AgentEvent(EventType.TOOL_CALL, tool_call.name)

                result = await self._execute_tool(state, tool_call)

                # 中断：持久化路由信息 + 剩余工具，仅发最小字段给前端
                if result.interrupt:
                    remaining = [tc.to_dict() for tc in tool_calls[i + 1:]]
                    self._persist_interrupt(state, tool_call, result, remaining)
                    await self._plugin_manager.run_hooks(
                        PluginHook.ON_INTERRUPT, state,
                        data={"interrupt": result.interrupt},
                    )
                    await self._checkpoint(state, checkpoint)
                    yield AgentEvent(EventType.INTERRUPT, result.interrupt.to_client())
                    return

                await self._commit_completed_tool_call(
                    state,
                    tool_call,
                    result,
                    checkpoint,
                )

    @staticmethod
    async def _checkpoint(
        state: AgentState,
        checkpoint: CheckpointCallback | None,
    ) -> None:
        """在启用持久化的执行入口中保存工具进度。"""
        if checkpoint is not None:
            await checkpoint(state, checkpoint_type="intermediate")

    # ==================== LLM 调用 ====================

    async def _call_llm(
        self,
        state: AgentState,
        runtime_context: RuntimeContext,
        checkpoint: CheckpointCallback | None = None,
    ) -> AsyncIterator[StreamChunk]:
        """调用 LLM，保留文本、工具调用和终止原因。"""
        tools = self._tool_manager.get_openai_tools()
        produced = False
        for attempt in range(2):
            prepared = await self._model_input.prepare(
                state=state,
                system_prompt=self._prompt_source.system_prompt,
                tools=tools,
                turn_context=runtime_context,
            )
            if prepared.state_changed:
                await self._checkpoint(state, checkpoint)
            try:
                async for chunk in self._llm_client.astream(
                    prepared.messages, tools=tools or None
                ):
                    produced = produced or bool(chunk.content or chunk.tool_call)
                    if chunk.finish_reason == "content_filter":
                        raise RuntimeError("触发 API 内容过滤")
                    yield chunk
                return
            except Exception as exc:
                text = str(exc).lower()
                context_error = "context_length_exceeded" in text or "maximum context" in text
                if attempt or produced or not context_error:
                    raise
                self._context_strategy.budget.raise_after_context_error()

    # ==================== 工具执行 ====================

    async def _execute_tool(
        self,
        state: AgentState,
        tool_call: ToolCall,
        resume_data: dict | None = None,
        resume_state: dict | None = None,
    ) -> ToolResult:
        """执行单个工具（支持中断后恢复执行：传入 resume_data + resume_state）"""
        await self._plugin_manager.run_hooks(
            PluginHook.BEFORE_TOOL, state,
            data={"tool_call": tool_call.to_dict()},
        )

        tool = self._tool_manager.get(tool_call.name)
        if not tool:
            logger.warning("工具 '%s' 不存在", tool_call.name)
            return ToolResult.error(f"工具 '{tool_call.name}' 不存在")

        context = ToolContext(
            session_id=state.session_id,
            state=state,
            emit_event=self._emit_event,
            get_checkpoint=state.to_checkpoint,
            set_checkpoint=lambda d: state.update_extra(d),
            model_name=self._prompt_source.model_name,
            resume_data=resume_data,
            resume_state=resume_state,
        )

        try:
            result = await tool.execute(tool_call.args, context)
        except Exception as e:
            logger.error("工具 '%s' 执行失败: %s", tool_call.name, e)
            result = ToolResult.error(str(e))

        result = self._tool_result_normalizer.normalize(result)

        await self._plugin_manager.run_hooks(
            PluginHook.AFTER_TOOL, state,
            data={"tool_call": tool_call.to_dict(), "result": result},
        )

        return result

    async def _commit_completed_tool_call(
        self,
        state: AgentState,
        tool_call: ToolCall,
        result: ToolResult,
        checkpoint: CheckpointCallback | None,
    ) -> None:
        """完整提交一个已经执行完成的工具调用。

        工具消息、pending 状态和批次末尾图片会在同一个 checkpoint 前同步
        更新，避免持久化“工具批次已结束但图片尚未注入”的中间状态。

        Args:
            state: 当前 Agent 状态。
            tool_call: 已执行完成的工具调用。
            result: 工具执行结果。
            checkpoint: intermediate checkpoint 持久化回调。
        """
        self._append_tool_result(state, tool_call, result)
        state.remove_pending_tool_call(tool_call.id)
        if not state.pending_tool_calls:
            state.flush_deferred_tool_images()
        await self._checkpoint(state, checkpoint)

    def _append_tool_result(
        self,
        state: AgentState,
        tool_call: ToolCall,
        result: ToolResult,
    ) -> None:
        """追加工具结果槽位，并把图片暂存到当前批次的延迟队列。"""
        if result.state_updates:
            state.update_extra(result.state_updates)

        state.add_tool_message(
            content=result.content,
            tool_name=tool_call.name,
            tool_call_id=tool_call.id,
            compressible=bool(getattr(self._tool_manager.get(tool_call.name), "compressible", False)),
        )
        tool_message_id = state.messages[-1]["_message_id"]

        image_urls = list(result.image_urls)
        if result.image_url and result.image_url not in image_urls:
            image_urls.insert(0, result.image_url)
        if image_urls:
            state.deferred_tool_images.append({
                "tool_call_id": tool_call.id,
                "tool_name": tool_call.name,
                "associated_message_id": tool_message_id,
                "image_message_name": result.image_message_name or SCREENSHOT_MESSAGE_NAME,
                "image_urls": image_urls,
            })

    def _persist_interrupt(
        self,
        state: AgentState,
        tool_call: ToolCall,
        result: ToolResult,
        pending_actions: list[dict],
    ) -> None:
        """把中断的完整路由信息写入 state，供跨请求恢复。"""
        interrupt = result.interrupt
        interrupt.tool_name = tool_call.name
        interrupt.tool_call_id = tool_call.id
        interrupt.tool_args = tool_call.args
        interrupt.resume_state = result.resume_state
        state.set_interrupt(
            interrupt_data=interrupt.to_state(),
            pending_actions=pending_actions,
        )
        state.clear_pending_tool_calls()

    # ==================== 助手消息 ====================

    def _add_assistant_message(
        self,
        state: AgentState,
        content: str,
        tool_calls: list[ToolCall],
    ) -> None:
        """添加助手消息到状态"""
        if tool_calls:
            msg = AssistantMessageWithTools(
                role=MessageRole.ASSISTANT,   # 必须用枚举，否则 to_openai_format 取 .value 崩溃
                content=content,
                tool_calls=tool_calls,
            )
            state.add_message(msg)
            state.updated_at = msg.timestamp
        else:
            state.add_assistant_message(content)
