"""准备子身份、父子消息和结果业务，执行资源由所属会话管理。"""
from collections.abc import Callable

from dataclasses import asdict
from datetime import datetime, timezone
from uuid import uuid4

from app.agent.runtime.subagent_requests import CreateSubagentRequest
from app.agent.state.manager import SessionStateManager
from app.crud.subagent_dao import SubagentDao
from app.errors import ResourceNotFoundError
from app.crud.task_record_dao import TaskRecordDao
from app.schemas.runtime_types import AgentRunOutcome, AgentRunOutcomeKind, InboxKind, TaskStatus, TurnInterruptReason
from app.schemas.subagent_types import SubagentDefinition, SubagentRecord, TaskRecordKind
from app.services.agent_identity_service import AgentIdentityService


class SubagentService:
    """集中子身份和结果的持久业务，供运行层组织执行流程。"""

    def __init__(self, definitions: SubagentDao, results: TaskRecordDao, identity: AgentIdentityService,
                 *, manager_factory: Callable[[SubagentDefinition], SessionStateManager]) -> None:
        """注入共享持久化与身份资格规则。

        Args:
            definitions (SubagentDao): 子身份及显式交互的事务存储。
            results (TaskRecordDao): 执行结果及通知的事务存储。
            identity (AgentIdentityService): 共享的身份资格查询对象。
            manager_factory (Callable[[SubagentDefinition], SessionStateManager]): 按实际子定义创建独立输入及历史对象的工厂。
        """
        self.definitions, self.results, self.identity = definitions, results, identity
        self._manager_factory = manager_factory

    async def create(self, request: CreateSubagentRequest) -> tuple[SubagentDefinition, bool]:
        """构造稳定定义并保存初始任务，重试返回原受理的身份。

        Args:
            request (CreateSubagentRequest): 已由运行入口绑定来源的具体请求。
        """
        definition = SubagentDefinition(uuid4().hex, request.source.session_id, request.name, request.system_prompt,
            request.tool_whitelist, request.if_retain_context, datetime.now(timezone.utc))
        saved = await self.definitions.create_with_command(definition, request.prompt,
                                                           f"{request.source.run_id}:{request.source.tool_call_id}")
        return saved, saved.subagent_id == definition.subagent_id

    async def finish_run(self, run_id: str, outcome: AgentRunOutcome, *, notify: bool) -> bool:
        """把执行结果映射为首次终态和报告，运行层在提交后发布记录。

        Args:
            run_id (str): 本次需要提交终态与报告的结果段编号。
            outcome (AgentRunOutcome): 原执行产生的正文、结束原因或失败信息。
            notify (bool): 是否允许随终态提交父收件通知。
        """
        status = (TaskStatus.COMPLETED if outcome.kind == AgentRunOutcomeKind.COMPLETED else
                  TaskStatus.FAILED if outcome.kind in {AgentRunOutcomeKind.FAILED, AgentRunOutcomeKind.SAVE_FAILED} else TaskStatus.STOPPED)
        return await self.results.finish_once(run_id, status, result=outcome.result, error=outcome.error, notify=notify) is not None

    async def get_details(self, session_id: str, subagent_id: str, interaction_limit: int) -> dict[str, object]:
        """查询归属内的定义、交互和最终报告，实时摘要交由运行层合并。

        Args:
            session_id (str): 本次操作所属的父会话编号。
            subagent_id (str): 实际执行的子身份，父执行时为空。
            interaction_limit (int): 详情中最多返回的显式交互条数。

        Returns:
            dict[str, object]: 保存的子身份、交互和结果记录。

        Raises:
            ValueError: 交互数量小于 1。
            ResourceNotFoundError: 子身份不存在或属于其他会话。
        """
        if interaction_limit < 1:
            raise ValueError("interaction_limit 必须为正数")
        record = await self.definitions.get(session_id, subagent_id)
        if record is None:
            raise ResourceNotFoundError("子 Agent 不存在或不属于当前会话")
        definition = record.definition
        results = [asdict(item) for item in await self.results.list(session_id)
                   if item.subagent_id == subagent_id and item.kind == TaskRecordKind.SUBAGENT
                   and item.status != TaskStatus.RUNNING and item.end_reason != TurnInterruptReason.STEER]
        interactions = await self.definitions.recent_interactions(subagent_id, interaction_limit)
        return {"subagent_id": subagent_id, "name": definition.name, "system_prompt": definition.system_prompt,
                "if_retain_context": definition.if_retain_context, "created_at": definition.created_at.isoformat(),
                "tool_whitelist": [item.stable_key for item in sorted(definition.tool_whitelist, key=lambda item: item.stable_key)],
                "context_available": await self.identity.read_availability(session_id, subagent_id),
                "recent_interactions": [{**asdict(item), "created_at": item.created_at.isoformat()} for item in interactions],
                "results": results}

    async def recover(self) -> tuple[SubagentRecord, ...]:
        """启动时整理旧执行和历史，返回可再次受派的子身份。"""
        orphaned: set[str] = set()
        for run_id in await self.results.running_ids(TaskRecordKind.SUBAGENT):
            previous = await self.results.get(run_id)
            orphaned.add(previous.subagent_id)
            await self.results.finish_once(run_id, TaskStatus.ORPHANED,
                error="应用重启，旧子执行已终止，不会自动续跑",
                notify=await self.identity.read_availability(previous.session_id, previous.subagent_id))
        available = []
        for record in await self.definitions.list_records():
            definition = record.definition
            manager = self._manager_factory(definition)
            try:
                batch = await manager.read_activation_batch()
                if definition.subagent_id not in orphaned and any(item.kind == InboxKind.AGENT_COMMAND for item in batch):
                    run_id = uuid4().hex
                    await self.results.start_subagent_run(definition, run_id, None)
                    await self.results.finish_once(run_id, TaskStatus.ORPHANED,
                        error="应用重启，已受理的子指令已终止，不会自动续跑",
                        notify=await self.identity.read_availability(definition.session_id, definition.subagent_id))
                await manager.finalize_abandoned_turn()
                if not definition.if_retain_context or not await self.identity.read_availability(definition.session_id, definition.subagent_id):
                    await self.definitions.retire(definition.subagent_id)
                else:
                    await manager.discard_pending_commands()
                    available.append(record)
            finally:
                await manager.close()
        return tuple(available)
