"""从现有配置和子定义读取身份资格，供受理及执行保护点复用。"""

from app.crud.chat_settings_dao import ChatSettingsDao
from app.crud.subagent_dao import SubagentDao


class AgentIdentityService:
    """依据父配置和子废弃时间统一判断身份资格。

    Attributes:
        settings (ChatSettingsDao): 父身份存在性的权威来源。
        subagents (SubagentDao): 子归属与废弃时间的权威来源。
    """

    def __init__(self, settings: ChatSettingsDao, subagents: SubagentDao) -> None:
        """接收应用共用的配置与子定义存储。

        Args:
            settings (ChatSettingsDao): 父角色配置的权威存储。
            subagents (SubagentDao): 子身份、消息和报告的共享业务对象。
        """
        self.settings, self.subagents = settings, subagents

    async def read_availability(self, session_id: str, subagent_id: str | None = None) -> bool:
        """读取当前资格，调用方的受理锁决定检查与登记是否连续。

        Args:
            session_id (str): 本次操作所属的父会话编号。
            subagent_id (str | None): 实际执行的子身份，父执行时为空。
        """
        if not self.settings.exists(session_id):
            return False
        if subagent_id is None:
            return True
        record = await self.subagents.get(session_id, subagent_id)
        return record is not None and record.retired_at is None

    async def require_available(self, session_id: str, subagent_id: str | None = None) -> None:
        """在新工作受理或执行前核对父存在性、子归属和废弃时间。

        Args:
            session_id (str): 本次操作所属的父会话编号。
            subagent_id (str | None): 实际执行的子身份，父执行时为空。
        """
        if not await self.read_availability(session_id, subagent_id):
            raise RuntimeError("父 Agent 配置已删除或子 Agent 已废弃、归属不匹配")
