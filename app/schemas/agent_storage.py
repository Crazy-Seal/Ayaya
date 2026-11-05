"""固定主会话和子 Agent 的执行表范围，模型输入不能指定表名。"""

from enum import StrEnum


class AgentStorageScope(StrEnum):
    """选择同一数据库中的主执行表或子执行表。"""

    MAIN = "main"
    SUBAGENT = "subagent"

    @property
    def checkpoints_table(self) -> str:
        """返回保存当前范围 AgentState 的固定表名。"""
        return "subagent_checkpoints" if self == self.SUBAGENT else "checkpoints"

    @property
    def messages_table(self) -> str:
        """返回保存当前范围协议消息的固定表名。"""
        return "subagent_conversation_messages" if self == self.SUBAGENT else "conversation_messages"

    @property
    def inbox_table(self) -> str:
        """返回当前范围的待交付输入表名。"""
        return "subagent_session_inbox" if self == self.SUBAGENT else "session_inbox"
