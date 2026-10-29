"""
执行管道

负责编排 LLM 调用和工具执行循环。

中断/恢复采用「可持久化」模型：被中断工具的全部路由信息（tool_name/
tool_call_id/tool_args/resume_state）写入 state.interrupt_data，使恢复能
在另一个请求（甚至进程重启后）重入同一工具——无需依赖存活的协程。
"""
from contextlib import aclosing
from app.agent.models.contracts import ModelFinishReason, StreamChunk

import logging
from typing import Any, AsyncIterator, Protocol
from app.schemas.desktop import DesktopOperations, StopRequested

from app.schemas.runtime_types import (
    AUTHORIZATION_GRANTED_KIND, ApprovalDecisionKind, DESKTOP_CHANNEL,
    InterruptKind,
)
from app.agent.context import ModelInputCoordinator, RuntimeContext
from app.agent.state.manager import SessionStateManager
from app.agent.runtime.turn_control import TurnInterrupted
from app.agent.core.events import EventType, AgentEvent
from app.agent.core.plugin_manager import PluginManager
from app.agent.core.response_filter import AssistantTimePrefixFilter
from app.agent.core.tool_manager import ToolManager
from app.agent.state.messages import ProviderData
from app.agent.models.llm_client import LLMClient
from app.agent.plugins.base import PluginHook
from app.agent.state.messages import (
    HumanMessage,
    ToolCall,
)
from app.agent.state import AgentState, TemporaryContextItem
from app.agent.state.time import format_local_time, utc_now
from app.agent.tools.core.runtime import ToolRuntimeFactory
from app.agent.tools.core.base import BaseTool, InterruptEvent, ToolContext, ToolResult
from app.agent.tools.results.normalizer import ToolResultNormalizer
from app.agent.tools.authorization.manager import ToolAuthorization, descriptor_for_tool
from app.agent.tools.authorization.models import AuthorizationRequired, ToolInvocation

logger = logging.getLogger(__name__)


class SystemPromptSource(Protocol):
    """Pipeline 构建模型消息所需的最小配置接口。"""

    system_prompt: str


class ExecutionPipeline:
    """执行模型与工具循环，在安全点响应停止信号并保留完整工具协议。

    Attributes:
        _prompt_source (SystemPromptSource): 当前模型循环使用的系统提示来源。
        _llm_client (LLMClient): 统一模型调用客户端。
        _tool_manager (ToolManager): 本地及 MCP 工具注册管理器。
        _plugin_manager (PluginManager): 插件生命周期与执行钩子的分发器。
        _tool_result_normalizer (ToolResultNormalizer): 工具输出与媒体的统一规范化器。
        _model_input (ModelInputCoordinator): 模型消息投影、上下文压缩和溢出恢复协调器。
        _tool_authorization (ToolAuthorization | None): 工具单次授权验证器。
        _tool_runtime_factory (ToolRuntimeFactory | None): 按工具、轮次及调用编号创建运行时。
    """

    def __init__(
        self,
        *,
        prompt_source: SystemPromptSource,
        llm_client: LLMClient,
        tool_manager: ToolManager,
        plugin_manager: PluginManager,
        model_input: ModelInputCoordinator,
        tool_authorization: ToolAuthorization | None = None,
        tool_runtime_factory: ToolRuntimeFactory | None = None,
        desktop: DesktopOperations | None = None,
    ) -> None:
        """装配模型输入、授权和任务启动端口，管道不持有渠道连接。

        Args:
            prompt_source (SystemPromptSource): 当前模型循环使用的系统提示来源。
            llm_client (LLMClient): 统一模型调用客户端。
            tool_manager (ToolManager): 本地及 MCP 工具注册管理器。
            plugin_manager (PluginManager): 插件生命周期与执行钩子的分发器。
            model_input (ModelInputCoordinator): 模型消息投影、上下文压缩和溢出恢复协调器。
            tool_authorization (ToolAuthorization | None): 工具单次授权验证器。
            tool_runtime_factory (ToolRuntimeFactory | None): 按工具、轮次及调用编号创建运行时。
            desktop (DesktopOperations | None): 应用共享桌面接口，管道只注入、不负责关闭。
        """
        self._prompt_source = prompt_source
        self._llm_client = llm_client
        self._tool_manager = tool_manager
        self._plugin_manager = plugin_manager
        self._tool_result_normalizer = ToolResultNormalizer()
        self._model_input = model_input
        self._tool_authorization = tool_authorization
        self._tool_runtime_factory = tool_runtime_factory
        self._desktop = desktop

    # ==================== 主入口 ====================

    async def execute(
        self,
        state: AgentState,
        session_state_manager: SessionStateManager | None = None,
        runtime_context: RuntimeContext | None = None,
        *, receive_task_notifications: bool = False,
        activation_data: dict | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """确定本轮真人输入标识，运行调用钩子后进入模型工具循环。

        Args:
            state (AgentState): 当前 Agent 状态。
            session_state_manager (SessionStateManager | None): 借用的会话状态业务对象，不负责关闭。
            runtime_context (RuntimeContext | None): 本轮临时上下文，不写入持久消息。
            receive_task_notifications (bool): 是否在安全点查询待交付任务通知。
            activation_data (dict | None): 输入提交前钩子准备的数据，输入保存成功后交给 ON_INVOKE。

        Yields:
            AgentEvent: 按执行顺序产生的 Agent 事件。
        """
        runtime_context = runtime_context or RuntimeContext.capture()
        if state.messages and isinstance(state.messages[-1], HumanMessage):
            runtime_context.latest_human_message_id = state.messages[-1].message_id
        # 1. 输入已经保存，补充本轮运行数据，再让插件启动准备好的工作并检索资料。
        # 1.1. 共用输入前置钩子的字典；审批恢复直接进入原循环，沿用已保存进度。
        hook_data = activation_data if activation_data is not None else {}
        hook_data["runtime_context"] = runtime_context
        await self._plugin_manager.run_hooks(
            PluginHook.ON_INVOKE, state, data=hook_data,
        )
        # 显式等待下层迭代关闭，保证响应和借用资源在返回前完成清理。
        async with aclosing(self._run_loop(
            state, session_state_manager, runtime_context, receive_task_notifications=receive_task_notifications
        )) as managed_events:
            async for event in managed_events:
                yield event

    async def resume_tools(
        self,
        state: AgentState,
        resume_data: dict,
        session_state_manager: SessionStateManager | None = None,
        runtime_context: RuntimeContext | None = None,
        *, receive_task_notifications: bool = False,
    ) -> AsyncIterator[AgentEvent]:
        """先重入中断工具，再处理剩余调用，最后继续模型循环。

        Args:
            state (AgentState): 当前 Agent 状态。
            resume_data (dict): 当前等待工具的人工审批决定。
            session_state_manager (SessionStateManager | None): 借用的会话状态业务对象，不负责关闭。
            runtime_context (RuntimeContext | None): 本轮临时上下文，不写入持久消息。
            receive_task_notifications (bool): 是否在安全点查询待交付任务通知。

        Yields:
            AgentEvent: 按执行顺序产生的 Agent 事件。
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
        resumed_calls.extend(pending_actions)
        state.set_pending_tool_calls(resumed_calls)

        # 1. 重入被中断的工具（用持久化的路由信息重建 ToolCall）
        if tool_name:
            interrupted_call = resumed_calls[0]
            if runtime_context and runtime_context.turn_control:
                runtime_context.turn_control.start_tool()
            result = await self._execute_tool(
                state,
                interrupted_call,
                resume_data=resume_data,
                resume_state=interrupt_data.get("resume_state", {}),
                origin_run_id=runtime_context.turn_control.run_id
                if runtime_context and runtime_context.turn_control else "",
                stop_requested=runtime_context.turn_control.is_interrupted
                if runtime_context and runtime_context.turn_control else None,
            )

            # 恢复执行时又触发了新的中断
            if result.interrupt:
                yield await self._commit_interrupt(
                    state, interrupted_call, result.interrupt,
                    result.resume_state, pending_actions, session_state_manager,
                )
                return

            yield await self._commit_completed_tool_call(
                state,
                interrupted_call,
                result,
                session_state_manager,
            )
            if runtime_context and runtime_context.turn_control:
                runtime_context.turn_control.check()

        # 2. 执行中断时尚未轮到的剩余工具
        for i, tool_call in enumerate(pending_actions):
            if runtime_context and runtime_context.turn_control:
                runtime_context.turn_control.start_tool()
            yield AgentEvent(EventType.TOOL_CALL, tool_call.name)
            if runtime_context and runtime_context.turn_control:
                runtime_context.turn_control.check()

            result = await self._execute_tool(
                state, tool_call,
                origin_run_id=runtime_context.turn_control.run_id
                if runtime_context and runtime_context.turn_control else "",
                stop_requested=runtime_context.turn_control.is_interrupted
                if runtime_context and runtime_context.turn_control else None,
            )
            if result.interrupt:
                yield await self._commit_interrupt(
                    state, tool_call, result.interrupt,
                    result.resume_state, pending_actions[i + 1:], session_state_manager,
                )
                return

            yield await self._commit_completed_tool_call(
                state,
                tool_call,
                result,
                session_state_manager,
            )
            if runtime_context and runtime_context.turn_control:
                runtime_context.turn_control.check()

        # 3. 回到 LLM 循环（不重跑 ON_INVOKE）
        runtime_context = runtime_context or RuntimeContext.capture()
        # 显式等待下层迭代关闭，保证响应和借用资源在返回前完成清理。
        async with aclosing(self._run_loop(
            state, session_state_manager, runtime_context, receive_task_notifications=receive_task_notifications
        )) as managed_events:
            async for event in managed_events:
                yield event

    # ==================== 核心循环 ====================

    async def _run_loop(
        self,
        state: AgentState,
        session_state_manager: SessionStateManager | None = None,
        runtime_context: RuntimeContext | None = None,
        *, receive_task_notifications: bool = False,
    ) -> AsyncIterator[AgentEvent]:
        """在每次模型调用前接收内部通知，流式输出文本并依次执行工具。

        Args:
            state (AgentState): 当前 Agent 状态。
            session_state_manager (SessionStateManager | None): 借用的会话状态业务对象，不负责关闭。
            runtime_context (RuntimeContext | None): 本轮临时上下文，不写入持久消息。
            receive_task_notifications (bool): 是否在安全点查询待交付任务通知。

        Yields:
            AgentEvent: 按执行顺序产生的 Agent 事件。

        Raises:
            RuntimeError: 模型未返回有效文本（finish_reason={reason}）。
        """
        while True:
            # 1. 模型调用前交付当前后台通知，然后设置 LLM 安全点和片段缓冲。
            if receive_task_notifications and session_state_manager is not None:
                await session_state_manager.deliver_pending(state)
            if runtime_context and runtime_context.turn_control:
                runtime_context.turn_control.start_llm()
            # 2. 执行 BEFORE_LLM 钩子，通知插件即将准备本次模型输入。
            await self._plugin_manager.run_hooks(
                PluginHook.BEFORE_LLM, state
            )

            # 3. 准备本次模型输出的正文、工具调用和时间前缀缓冲。
            accumulated_content = ""
            tool_calls: list[ToolCall] = []
            finish_reason: ModelFinishReason | None = None
            provider_data: ProviderData | None = None
            time_prefix_filter = AssistantTimePrefixFilter()

            try:
                runtime_context = runtime_context or RuntimeContext.capture()
                # 3.1. 逐项读取模型输出，等待任务可被停止，但会话收尾任务仍保留。
                llm_stream = self._call_llm(state, runtime_context, session_state_manager)
                try:
                    while True:
                        try:
                            chunk = (
                                await runtime_context.turn_control.next_llm_chunk(llm_stream)
                                if runtime_context.turn_control else await anext(llm_stream)
                            )
                        except StopAsyncIteration:
                            break
                        if chunk.content:
                            filtered_content = time_prefix_filter.feed(chunk.content)
                            if filtered_content:
                                accumulated_content += filtered_content
                                if runtime_context.turn_control:
                                    runtime_context.turn_control.append(filtered_content)
                                yield AgentEvent(EventType.TEXT_CHUNK, filtered_content)
                                if runtime_context.turn_control:
                                    runtime_context.turn_control.check()
                        if chunk.tool_call:
                            tool_calls.append(chunk.tool_call)
                        if chunk.provider_data is not None:
                            provider_data = chunk.provider_data
                        if chunk.finish_reason:
                            finish_reason = chunk.finish_reason
                finally:
                    await llm_stream.aclose()

                trailing_content = time_prefix_filter.finish()
                if trailing_content:
                    accumulated_content += trailing_content
                    if runtime_context.turn_control:
                        runtime_context.turn_control.append(trailing_content)
                    yield AgentEvent(EventType.TEXT_CHUNK, trailing_content)
                    if runtime_context.turn_control:
                        runtime_context.turn_control.check()

                if not tool_calls and not accumulated_content.strip():
                    reason = finish_reason or ModelFinishReason.UNKNOWN
                    raise RuntimeError(f"模型未返回有效文本（finish_reason={reason}）")
            # 3.2. 先冲刷时间前缀过滤器的剩余正文，确保已展示片段能够保存。
            except TurnInterrupted:
                trailing_content = time_prefix_filter.finish()
                if trailing_content:
                    accumulated_content += trailing_content
                    if runtime_context.turn_control:
                        runtime_context.turn_control.append(trailing_content)
                    yield AgentEvent(EventType.TEXT_CHUNK, trailing_content)
                raise
            except Exception as e:
                trailing_content = time_prefix_filter.finish()
                if trailing_content:
                    accumulated_content += trailing_content
                    yield AgentEvent(EventType.TEXT_CHUNK, trailing_content)
                logger.error("LLM 调用失败: %s", e)
                await self._plugin_manager.run_hooks(
                    PluginHook.ON_ERROR, state, data={"error": e}
                )
                yield AgentEvent(EventType.ERROR, str(e))
                return

            # 4. AFTER_LLM 钩子前后都检查打断，避免收到停止后仍记录新步骤。
            if runtime_context.turn_control:
                runtime_context.turn_control.check()
            await self._plugin_manager.run_hooks(
                PluginHook.AFTER_LLM, state,
                data={"content": accumulated_content, "tool_calls": tool_calls},
            )
            if runtime_context.turn_control:
                runtime_context.turn_control.check()

            # 5. 将 Assistant 步骤加入状态，工具前写入 checkpoint 后标记已保存。
            if accumulated_content or tool_calls:
                self._add_assistant_message(
                    state, accumulated_content, tool_calls,
                    origin_channel=runtime_context.origin_channel, provider_data=provider_data,
                )
                if runtime_context.turn_control:
                    runtime_context.turn_control.partial_persisted = True
                    # 5.1. 内存记录不等于事务已经落盘，关闭收尾通过身份核对实际 checkpoint。
                    runtime_context.turn_control.partial_message_id = state.messages[-1].message_id

            if tool_calls:
                await self._checkpoint(state, session_state_manager)

            # 6. 没有工具调用时结束本轮，由 Agent 执行最终提交。
            if not tool_calls:
                break

            # 7. 逐个执行工具；提示事件之后再检查停止，副作用开始后只等待真实结果。
            for i, tool_call in enumerate(tool_calls):
                if runtime_context.turn_control:
                    runtime_context.turn_control.start_tool()
                yield AgentEvent(EventType.TOOL_CALL, tool_call.name)
                if runtime_context.turn_control:
                    runtime_context.turn_control.check()

                result = await self._execute_tool(
                    state, tool_call,
                    stop_requested=runtime_context.turn_control.is_interrupted
                    if runtime_context.turn_control else None,
                    origin_run_id=runtime_context.turn_control.run_id
                    if runtime_context.turn_control else "",
                )

                # 中断：持久化路由信息 + 剩余工具，仅发最小字段给前端
                if result.interrupt:
                    remaining = tool_calls[i + 1:]
                    yield await self._commit_interrupt(
                        state, tool_call, result.interrupt,
                        result.resume_state, remaining, session_state_manager,
                    )
                    return

                yield await self._commit_completed_tool_call(
                    state,
                    tool_call,
                    result,
                    session_state_manager,
                )
                if runtime_context.turn_control:
                    runtime_context.turn_control.check()

    @staticmethod
    async def _checkpoint(
        state: AgentState,
        session_state_manager: SessionStateManager | None,
    ) -> None:
        """在启用持久化的执行入口中保存工具进度。

        Args:
            state (AgentState): 当前会话的内存 AgentState；保存由调用方负责。
            session_state_manager (SessionStateManager | None): 执行管道借用的会话状态业务对象。
        """
        if session_state_manager is not None:
            await session_state_manager.save_progress(state)

    # ==================== LLM 调用 ====================

    async def _call_llm(
        self,
        state: AgentState,
        runtime_context: RuntimeContext,
        session_state_manager: SessionStateManager | None = None,
    ) -> AsyncIterator[StreamChunk]:
        """调用 LLM，保留文本、工具调用和终止原因。

        Args:
            state (AgentState): 当前会话的内存 AgentState；保存由调用方负责。
            runtime_context (RuntimeContext): 本轮来源、真人输入编号和停止信号。
            session_state_manager (SessionStateManager | None): 执行管道借用的会话状态业务对象。
        """
        tools = self._tool_manager.get_tools()
        produced = False
        # 1. 每次请求刷新当前时间，预算检查和超限压缩重试继续使用这份临时条目。
        state.set_temporary_context("core.current_time", TemporaryContextItem(
            title="当前日期时间", text=format_local_time(utc_now()),
        ))
        prepared = await self._model_input.prepare(
            state=state,
            system_prompt=self._prompt_source.system_prompt,
            tools=tools,
            turn_context=runtime_context,
        )
        recovery = None
        try:
            while True:
                latest_prompt_tokens = 0
                has_valid_output = False
                if prepared.state_changed:
                    await self._checkpoint(state, session_state_manager)
                try:
                    stream = self._llm_client.astream(prepared.messages, tools=tools or None)
                    try:
                        async for chunk in stream:
                            if chunk.usage and chunk.usage.prompt_tokens:
                                latest_prompt_tokens = chunk.usage.prompt_tokens
                            produced = produced or bool(chunk.content or chunk.tool_call)
                            has_valid_output = has_valid_output or bool(
                                chunk.tool_call or (chunk.content and chunk.content.strip())
                            )
                            if chunk.finish_reason == ModelFinishReason.REFUSED:
                                raise RuntimeError("触发 API 内容过滤")
                            yield chunk
                    finally:
                        # 1. 停止发生在正文交付之后时，关闭当前网络响应，再交还执行控制。
                        await stream.aclose()
                    if latest_prompt_tokens and has_valid_output:
                        self._model_input.observe_usage(
                            state=state,
                            prepared=prepared,
                            prompt_tokens=latest_prompt_tokens,
                        )
                    return
                except Exception as exc:
                    if produced or not self._model_input.is_context_overflow(exc):
                        raise
                    if recovery is None:
                        recovery = self._model_input.recover_context_overflow(
                            state=state,
                            system_prompt=self._prompt_source.system_prompt,
                            tools=tools,
                            turn_context=runtime_context,
                        )
                    try:
                        prepared = await anext(recovery)
                    except StopAsyncIteration:
                        raise exc
        finally:
            if recovery is not None:
                await recovery.aclose()

    # ==================== 工具执行 ====================

    async def _execute_tool(
        self,
        state: AgentState,
        tool_call: ToolCall,
        resume_data: dict | None = None,
        resume_state: dict | None = None,
        origin_run_id: str = "",
        stop_requested: StopRequested | None = None,
    ) -> ToolResult:
        """复核工具及调用授权，注入任务启动上下文并执行一次工具。

        Args:
            state (AgentState): 当前 Agent 状态。
            tool_call (ToolCall): 当前执行的工具调用。
            resume_data (dict | None): 当前等待工具的人工审批决定。
            resume_state (dict | None): 中断前保存的工具中间状态。
            origin_run_id (str): 发起进程任务的运行轮次标识。
            stop_requested (StopRequested | None): 当前轮次已受理停止或引导时返回 True。

        Returns:
            ToolResult: 当前工具直接执行的结果或统一授权等待。
        """
        # 1. 查找当前工具，缺失时返回可闭合调用的错误结果。
        tool = self._tool_manager.get(tool_call.name)
        if not tool:
            logger.warning("工具 '%s' 不存在", tool_call.name)
            return ToolResult(content=f"错误: 工具 '{tool_call.name}' 不存在")

        grant: dict | None = None
        prepared_action: dict | None = None
        tool_resume_state = resume_state
        effective_resume_data = resume_data
        descriptor = descriptor_for_tool(tool)
        hooks_started = False

        # 2. 使用既有统一授权：首次调用按配置决定，审批恢复核对已保存的动作快照。
        if self._tool_authorization is not None:
            envelope = resume_state or {}
            hooks_started = bool(envelope.get("hooks_started"))
            prepared_action = envelope.get("prepared_action")
            invocation = ToolInvocation(
                session_id=state.session_id,
                tool_call_id=tool_call.id,
                descriptor=descriptor,
                arguments=tool_call.args,
                prepared_action=prepared_action,
            )

            # 2.1. 恢复人工决定并保留原工具中间态；新权限变化可能再次要求审批。
            if "authorization_request" in envelope:
                outcome = self._tool_authorization.resolve_authorization(
                    invocation,
                    decision=self._normalize_authorization_decision(resume_data),
                    authorization_snapshot=envelope.get("authorization_request") or {},
                )
                if isinstance(outcome, ToolResult):
                    return outcome
                if isinstance(outcome, AuthorizationRequired):
                    return self._authorization_result(
                        outcome,
                        tool_resume_state=envelope.get("tool_resume_state") or {},
                        prepared_action=prepared_action,
                        hooks_started=hooks_started,
                    )
                grant = outcome.grant.to_dict() if outcome.grant else None
                tool_resume_state = envelope.get("tool_resume_state") or {}
                effective_resume_data = (
                    {"kind": AUTHORIZATION_GRANTED_KIND}
                    if prepared_action is not None
                    else None
                )
            else:
                outcome = self._tool_authorization.authorize(invocation)
                if isinstance(outcome, ToolResult):
                    return outcome
                if isinstance(outcome, AuthorizationRequired):
                    return self._authorization_result(outcome)
                grant = outcome.grant.to_dict() if outcome.grant else None

        # 3. BEFORE_TOOL 在该调用链只运行一次，恢复时沿用持久 hooks_started。
        if not hooks_started:
            await self._plugin_manager.run_hooks(
                PluginHook.BEFORE_TOOL, state,
                data={"tool_call": tool_call.to_dict()},
            )
            hooks_started = True

        # 4. 注入状态与任务端口，工具运行时按本次来源创建，任务由共享 Supervisor 持有，并记录来源 run 和工具调用。
        context = ToolContext(
            session_id=state.session_id,
            state=state,
            resume_data=effective_resume_data,
            resume_state=tool_resume_state,
            authorization_grant=grant,
            runtime=self._tool_runtime_factory(tool_call.name, origin_run_id, tool_call.id)
            if self._tool_runtime_factory is not None else None,
            desktop=self._desktop,
            stop_requested=stop_requested,
            origin_run_id=origin_run_id,
            origin_tool_call_id=tool_call.id,
            origin_tool_name=tool_call.name,
        )

        # 5. 执行工具并把普通异常转换为 ToolResult，再统一规范化过长输出与媒体。
        try:
            result = await tool.execute(tool_call.args, context)
        except Exception as e:
            logger.error("工具 '%s' 执行失败: %s", tool_call.name, e)
            result = ToolResult(content=f"错误: {e}")

        result = self._tool_result_normalizer.normalize(
            result,
            session_id=state.session_id,
            persist_overflow=tool.persist_oversized_result,
        )

        # 6. 只读准备得到具体动作后重新授权，获准后才重入准备完毕的工具。
        if self._tool_authorization is not None and result.prepared_action is not None:
            prepared_invocation = ToolInvocation(
                session_id=state.session_id,
                tool_call_id=tool_call.id,
                descriptor=descriptor,
                arguments=tool_call.args,
                prepared_action=result.prepared_action,
            )
            outcome = self._tool_authorization.authorize(prepared_invocation)
            if isinstance(outcome, ToolResult):
                return outcome
            if isinstance(outcome, AuthorizationRequired):
                return self._authorization_result(
                    outcome,
                    tool_resume_state=result.resume_state,
                    prepared_action=result.prepared_action,
                    hooks_started=hooks_started,
                )
            grant = outcome.grant.to_dict() if outcome.grant else None
            result = await self._call_prepared_tool(
                state,
                tool_call,
                tool,
                result.resume_state,
                grant,
                stop_requested=stop_requested,
                origin_run_id=origin_run_id,
            )
            prepared_action = prepared_invocation.prepared_action

        # 7. 已退役的执行中断不能重新进入等待；当前公开协议只支持统一审批。
        if result.interrupt and result.interrupt.type != InterruptKind.AUTHORIZATION_REQUEST:
            return ToolResult(content="错误: 工具返回不支持的中断；准备阶段只能请求只读准备数据")

        # 8. 只有工具实际完成才运行 AFTER_TOOL，等待中断不提前触发完成钩子。
        await self._plugin_manager.run_hooks(
            PluginHook.AFTER_TOOL, state,
            data={"tool_call": tool_call.to_dict(), "result": result},
        )
        return result

    @staticmethod
    def _authorization_result(
        outcome: AuthorizationRequired,
        *,
        tool_resume_state: dict | None = None,
        prepared_action: dict | None = None,
        hooks_started: bool = False,
    ) -> ToolResult:
        """把待审批结果封装为可恢复工具状态，保留动作与钩子是否已执行的事实。

        Args:
            outcome (AuthorizationRequired): 授权模块生成的待决请求。
            tool_resume_state (dict | None): 工具自身的中间态。
            prepared_action (dict | None): 已准备且待批准的动作。
            hooks_started (bool): 本次工具前置钩子是否已开始。

        Returns:
            ToolResult: 仅请求审批、不产生工具副作用的中断结果。
        """
        return ToolResult(
            content="",
            interrupt=outcome.interrupt,
            resume_state={
                "authorization_request": outcome.resume_state,
                "tool_resume_state": tool_resume_state or {},
                "prepared_action": prepared_action,
                "hooks_started": hooks_started,
            },
        )

    async def _call_prepared_tool(
        self,
        state: AgentState,
        tool_call: ToolCall,
        tool: BaseTool,
        resume_state: dict,
        grant: dict | None,
        stop_requested: StopRequested | None = None,
        origin_run_id: str = "",
    ) -> ToolResult:
        """使用已准备动作和当前授权重入工具，屏幕动作由注入的共享后端桌面接口直接执行。

        Args:
            state (AgentState): 当前 Agent 状态。
            tool_call (ToolCall): 当前执行的工具调用。
            tool (BaseTool): 当前准备或执行的工具实例。
            resume_state (dict): 中断前保存的工具中间状态。
            grant (dict | None): 绑定本次调用的授权快照。
            stop_requested (StopRequested | None): 当前轮次的只读停止检查。
            origin_run_id (str): 当前运行标识，保留后台任务的来源关联。

        Returns:
            ToolResult: 准备后获准的工具执行结果。
        """
        # 1. 使用已获准的具体动作和中间态重入工具，仍传入共享任务端口。
        context = ToolContext(
            session_id=state.session_id,
            state=state,
            resume_data={"kind": AUTHORIZATION_GRANTED_KIND},
            resume_state=resume_state,
            authorization_grant=grant,
            runtime=self._tool_runtime_factory(tool_call.name, origin_run_id, tool_call.id)
            if self._tool_runtime_factory is not None else None,
            origin_run_id=origin_run_id,
            origin_tool_call_id=tool_call.id,
            origin_tool_name=tool_call.name,
            desktop=self._desktop,
            stop_requested=stop_requested,
        )
        # 2. 普通工具异常转换为可展示结果，随后执行统一结果规范化。
        try:
            result = await tool.execute(tool_call.args, context)
        except Exception as exc:
            logger.error("工具 '%s' 执行失败: %s", tool_call.name, exc)
            result = ToolResult(content=f"错误: {exc}")
        return self._tool_result_normalizer.normalize(
            result,
            session_id=state.session_id,
            persist_overflow=tool.persist_oversized_result,
        )

    @staticmethod
    def _normalize_authorization_decision(resume_data: dict | None) -> dict[str, Any]:
        """沿用原有恢复格式，将布尔批准标记映射为具名用户决定。

        Args:
            resume_data (dict | None): 已知决定对象或现有 approved 布尔恢复数据。

        Returns:
            dict[str, Any]: 原决定对象或按原规则生成的批准、拒绝决定。
        """
        # 1. 已知决定原样返回，保留审批框引导的正文及其他已有数据。
        if resume_data and resume_data.get("kind") in {ApprovalDecisionKind.APPROVE, ApprovalDecisionKind.REJECT, ApprovalDecisionKind.STEER}:
            return resume_data
        # 2. 其他恢复数据继续使用原布尔约定，缺失或非 True 都表示拒绝。
        return {
            "kind": ApprovalDecisionKind.APPROVE
            if resume_data and resume_data.get("approved") is True
            else ApprovalDecisionKind.REJECT
        }


    async def _commit_completed_tool_call(
        self,
        state: AgentState,
        tool_call: ToolCall,
        result: ToolResult,
        session_state_manager: SessionStateManager | None,
    ) -> AgentEvent:
        """保存完整工具结果，成功后报告调用完成，供界面查询对应历史。

        完整工具结果和 pending 状态会在同一个 checkpoint 前同步
        更新，避免持久化“调用已闭合但结果图片尚未保存”的中间状态。

        Args:
            state (AgentState): 当前 Agent 状态。
            tool_call (ToolCall): 已执行完成的工具调用。
            result (ToolResult): 工具执行结果。
            session_state_manager (SessionStateManager | None): 中间进度的会话状态业务对象。

        Returns:
            AgentEvent: 携带调用编号、工具名和结果消息编号的工具完成事件；保存失败时抛出异常。
        """
        # 1. 完整结果立即进入状态；不再为 API 排列拆出或延迟图片。
        message = state.record_tool_result(tool_call, result.content, compressible=bool(
            getattr(self._tool_manager.get(tool_call.name), "compressible", False)))
        if result.state_updates:
            state.update_extra(result.state_updates)
        await self._checkpoint(state, session_state_manager)
        # 2. 保存完成后才允许界面查询；消息编号让查询停在本次结果，后续回复仍由正文事件显示。
        return AgentEvent(EventType.TOOL_COMPLETED, {
            "tool_call_id": tool_call.id, "tool_name": tool_call.name, "message_id": message.message_id,
        })

    async def _commit_interrupt(
        self,
        state: AgentState,
        tool_call: ToolCall,
        interrupt: InterruptEvent,
        resume_state: dict,
        pending_actions: list[ToolCall],
        session_state_manager: SessionStateManager | None,
    ) -> AgentEvent:
        """统一中断迁移：写入路由、执行钩子、保存，再构造客户端事件。

        钩子的原地修改一并保存；保存失败时不产生中断事件。
        未配置会话状态管理器时仍支持独立执行 Pipeline。

        Args:
            state (AgentState): 当前会话的内存 AgentState；保存由调用方负责。
            tool_call (ToolCall): 本次模型请求的工具调用，包含编号、名称和参数。
            interrupt (InterruptEvent): 当前工具要求等待审批或桌面处理的中断事件。
            resume_state (dict): 工具继续执行所需的私有恢复材料。
            pending_actions (list[ToolCall]): 当前中断之后尚未执行的同批工具调用。
            session_state_manager (SessionStateManager | None): 执行管道借用的会话状态业务对象。
        """
        interrupt.tool_name = tool_call.name
        interrupt.tool_call_id = tool_call.id
        interrupt.tool_args = tool_call.args
        interrupt.resume_state = resume_state
        state.set_interrupt(
            interrupt_data=interrupt.to_state(),
            pending_actions=pending_actions,
        )
        state.clear_pending_tool_calls()
        await self._plugin_manager.run_hooks(
            PluginHook.ON_INTERRUPT, state, data={"interrupt": interrupt},
        )
        await self._checkpoint(state, session_state_manager)
        return AgentEvent(EventType.INTERRUPT, interrupt.to_client())

    # ==================== 助手消息 ====================

    def _add_assistant_message(
        self,
        state: AgentState,
        content: str,
        tool_calls: list[ToolCall],
        *,
        origin_channel: str = DESKTOP_CHANNEL,
        provider_data: ProviderData | None = None,
    ) -> None:
        """保存带入口来源的 Assistant 步骤，并登记需要闭合的工具调用。

        Args:
            state (AgentState): 当前 Agent 状态。
            content (str): Assistant 正文。
            tool_calls (list[ToolCall]): 本步模型请求的工具调用。
            origin_channel (str): 输入或本轮回复所属的渠道。
        """
        state.record_assistant_step(content, tool_calls, origin_channel=origin_channel, provider_data=provider_data)
