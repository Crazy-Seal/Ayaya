from app.crud.chat_history_dao import ChatHistoryDao


class ChatHistoryService:
    """为路由提供可见历史查询，数据库读取和消息转换交给 DAO。"""
    def __init__(self, chat_history_dao: ChatHistoryDao):
        """保存共享历史访问对象，构造时不查询数据库。

        Args:
            chat_history_dao (ChatHistoryDao): 把协议日志转换为界面聊天历史的数据访问对象。
        """
        self.chat_history_dao = chat_history_dao

    async def get_chat_history_data(self, session_id: str, start: int = 0, limit: int = 200) -> list[dict[str, str]]:
        """查询会话可见历史的一页消息，按聊天展示顺序返回。

        Args:
            session_id (str): 当前操作所属的会话编号。
            start (int): 可见历史的起始位置，从零开始。
            limit (int): 最多返回的记录数量。

        Returns:
            list[dict[str, str]]: 供 ChatHistoryItem 转换使用的消息字典列表。
        """
        return await self.chat_history_dao.list_chat_history_async(session_id, start, limit)

    async def get_chat_history_last_n(
        self, session_id: str, n: int = 100, *, through_message_id: str | None = None,
    ) -> list[dict[str, str]]:
        """查询最近可见历史，工具执行中刷新时只读到指定结果消息。

        Args:
            session_id (str): 当前操作所属的会话编号。
            n (int): 最多返回的可见消息数量。
            through_message_id (str | None): 可选的结果消息编号，包含该消息并排除其后的记录。

        Returns:
            list[dict[str, str]]: 最近消息按原顺序排列的字典列表。
        """
        return await self.chat_history_dao.list_chat_history_last_n_async(
            session_id, n, through_message_id=through_message_id,
        )
