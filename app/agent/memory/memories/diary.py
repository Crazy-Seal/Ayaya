"""每日记忆：从协议日志生成和检索日记。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from app.schemas.runtime_types import (
    DESKTOP_CHANNEL,
)
from app.agent.memory.config import MemoryConfig
from app.agent.memory.store.chat_history_store import ChatHistoryStore
from app.agent.memory.store.diary_sqlite_store import DiarySqliteStore
from app.agent.state.messages import (
    notification_text, text_content, format_tool_arguments,
    AssistantMessage,
    BaseMessage,
    HumanMessage,
    RuntimeInputMessage,
    ToolMessage,
)
from app.agent.models.llm_client import LLMClient, LLMConfig
from app.schemas.chat_settings import ChatSettings


# 同一进程中的记忆实例共用处理中记录，修改设置后重新创建实例仍能避免重复生成。
_pending_diaries: set[tuple[str, date]] = set()


@dataclass
class DiaryGeneration:
    """保存已确认需要生成的日记材料，并持有该会话该日期的处理标记。

    Attributes:
        session_id (str): 日记所属会话。
        target_date (date): 本次要生成日记的有效聊天日。
        messages (list[BaseMessage]): 按输入保存前历史范围读取的当日对话。
        max_history_id (int | None): 查询前情时沿用的历史最大编号。
        _released (bool): 是否已释放标记，保证重复清理不会影响后续重试。
    """

    session_id: str
    target_date: date
    messages: list[BaseMessage]
    max_history_id: int | None
    _released: bool = field(default=False, init=False, repr=False)

    def release(self) -> None:
        """准备后的提交失败或任务清理时释放标记，让后续激活能够重新检查。"""
        if not self._released:
            _pending_diaries.discard((self.session_id, self.target_date))
            self._released = True


class DiaryMemory:
    """从协议日志生成并检索每日记忆。

    Attributes:
        session_id (str): 共享 Ayaya 会话标识。
        config (MemoryConfig): 当前 Agent 配置。
        chat_settings (ChatSettings): 会话模型和插件配置。
        chat_history_store (ChatHistoryStore): 领域聊天历史查询端口。
        store (日记持久访问器): 该记忆模块的持久存取对象。
        llm (LLM 客户端): 用于记忆生成的模型客户端。
    """

    def __init__(
        self,
        session_id: str,
        config: MemoryConfig,
        chat_settings: ChatSettings,
        chat_history_store: ChatHistoryStore,
    ) -> None:
        """绑定会话、只读协议历史和日记存储，创建使用会话模型设置的日记生成客户端。

        Args:
            session_id (str): 当前操作所属的会话编号。
            config (MemoryConfig): 调用方提供的配置。
            chat_settings (ChatSettings): 当前会话的模型、角色及插件设置。
            chat_history_store (ChatHistoryStore): 只读查询已保存对话协议日志的对象。
        """
        self.session_id = session_id
        self.config = config
        self.chat_settings = chat_settings
        self.chat_history_store = chat_history_store
        self.store = DiarySqliteStore(config.sqlite_path)
        self.llm = LLMClient(LLMConfig(
            model=chat_settings.model_name,
            api_key=chat_settings.openai_api_key,
            base_url=chat_settings.openai_base_url,
            temperature=0.2,
            timeout=60.0,
        ))

    async def prepare_generation(
        self, today: date, *, max_history_id: int | None = None
    ) -> DiaryGeneration | None:
        """输入保存后检查最近已结束聊天日，只为尚无日记且有对话的日期准备生成材料。

        Args:
            today (date): 按配置时区及凌晨分界小时计算的当前有效日期。
            max_history_id (int | None): 本次提取开始前捕获的协议日志最大编号；排除之后到达的消息。

        Returns:
            DiaryGeneration | None: 持有处理中标记的生成材料；已有日记、正在处理或无历史时为空。
        """
        # 1. 只检查当前有效日期之前最近的聊天日，并使用输入提交前捕获的历史编号边界。
        target = await self.chat_history_store.get_last_chat_date(
            self.session_id, exclude_today=today, max_history_id=max_history_id
        )
        if target is None:
            return
        # 2. 检查与加入之间没有异步等待，其他激活会在数据库检查和模型生成期间直接跳过。
        key = (self.session_id, target)
        if key in _pending_diaries:
            return
        _pending_diaries.add(key)
        generation = None
        try:
            # 3. 已有日记或空对话只结束检查；需要生成时把固定日期和材料交给后台任务。
            if await self.has_diary(target):
                return
            messages = await self.chat_history_store.get_messages_by_date(
                self.session_id, target, max_history_id=max_history_id
            )
            if messages:
                generation = DiaryGeneration(self.session_id, target, messages, max_history_id)
            return generation
        finally:
            # 4. 跳过、查询失败或检查被取消时立即释放；成功准备后由提交方或任务清理释放。
            if generation is None:
                _pending_diaries.discard(key)

    async def add(self, date_obj: date, content: str) -> int:
        """把指定有效日期的日记正文交给 SQLite 存储并提交。

        Args:
            date_obj (date): 要保存或查询的日记有效日期。
            content (str): 当前处理的正文。

        Returns:
            int: 日记存储返回的最近插入编号。
        """
        return await self.store.add(self.session_id, date_obj, content)

    async def search(self, date_obj: date) -> str | None:
        """读取当前会话某个有效日期的日记正文。

        Args:
            date_obj (date): 要保存或查询的日记有效日期。

        Returns:
            str | None: 该日正文，没有记录时为空。
        """
        return await self.store.get(self.session_id, date_obj)

    async def has_diary(self, date_obj: date) -> bool:
        """查询当前会话指定有效日期是否已有日记，避免重复生成。

        Args:
            date_obj (date): 要保存或查询的日记有效日期。

        Returns:
            bool: 该日期已有日记时为 True。
        """
        return await self.store.exists(self.session_id, date_obj)

    async def search_diary(self, start: date, end: date) -> list[tuple[date, str]]:
        """读取当前会话包含起止日期的日记范围。

        Args:
            start (date): 查询范围的起始日期或时间。
            end (date): 查询范围的结束日期或时间。

        Returns:
            list[tuple[date, str]]: 按日期递增的日记日期与正文列表。
        """
        return await self.store.get_range(self.session_id, start, end)

    async def get_context(self, today: date) -> str:
        """读取当前有效日期之前最近的日记，按时间先后组成供模型参考的文本。

        Args:
            today (date): 按配置时区及凌晨分界小时计算的当前有效日期。

        Returns:
            str: 带日期标签的日记文本，没有历史时为空串。
        """
        diaries = await self.store.get_recent_before_date(
            self.session_id, today, n=self.config.recent_diaries_count
        )
        return "\n\n".join(
            f"【{day.isoformat()}】\n{content}" for day, content in reversed(diaries)
        )

    async def generate(
        self,
        target_date: date,
        messages: list[BaseMessage],
        *,
        max_history_id: int | None = None,
    ) -> None:
        """读取旧日记和更早对话作为前情，调用模型并保存日记，失败交给任务框架记录。

        Args:
            target_date (date): 需要生成日记的有效日期。
            messages (list[BaseMessage]): 按对话顺序排列的消息集合。
            max_history_id (int | None): 本次提取开始前捕获的协议日志最大编号；排除之后到达的消息。
        """
        # 1. 旧日记和更早对话仅提供前情，目标日消息才是本次需要记录的事实。
        previous_diaries = await self.store.get_recent_before_date(
            self.session_id,
            target_date,
            n=self.config.recent_diaries_count,
        )
        previous_messages = await self.chat_history_store.get_messages_before_date(
            self.session_id,
            target_date,
            limit=10,
            max_history_id=max_history_id,
        )
        prompt = self._build_diary_prompt(
            messages,
            target_date,
            previous_diaries=previous_diaries,
            previous_messages=previous_messages,
        )
        # 2. 模型成功返回后保存正文；任何一步失败均向上传播，由任务框架保存失败状态。
        response = await self.llm.ainvoke([HumanMessage(content=text_content(prompt))])
        await self.add(target_date, response.text)

    def _build_diary_prompt(
        self,
        messages: list[BaseMessage],
        target_date: date,
        *,
        previous_diaries: list[tuple[date, str]],
        previous_messages: list[BaseMessage],
    ) -> str:
        """根据角色设置和目标日历史构造日记提示词，明确区分前情参考与本次需要总结的内容。

        Args:
            messages (list[BaseMessage]): 按对话顺序排列的消息集合。
            target_date (date): 需要生成日记的有效日期。
            previous_diaries (list[tuple[date, str]]): 目标日期之前的日记，仅用于理解前情。
            previous_messages (list[BaseMessage]): 目标日期之前的最近对话，仅用于理解前情。

        Returns:
            str: 供日记生成模型使用的完整提示词。
        """
        target = target_date.isoformat()
        name = (
            self.chat_settings.name.strip()
            if self.chat_settings.name and self.chat_settings.name.strip()
            else "AI助手"
        )
        diary_context = self._format_diaries(previous_diaries)
        previous_context = (
            self._format_messages(previous_messages)
            if previous_messages else "此前暂无对话记录"
        )

        return f"""你是{name}，一个{self.chat_settings.feature.strip() + "的" if self.chat_settings.feature and self.chat_settings.feature.strip() else ""}{self.chat_settings.character.strip() if self.chat_settings.character and self.chat_settings.character.strip() else "AI助手"}，称呼用户为{self.chat_settings.address.strip() if self.chat_settings.address and self.chat_settings.address.strip() else "用户"}。
{self.chat_settings.characteristic.strip() if self.chat_settings.characteristic and self.chat_settings.characteristic.strip() else ""}

请以你的第一人称视角，基于以前的日记、{target}之前最后的对话记录和{target}当天的对话，把这一天写成一篇连续、自然、真实的日记，日记分为日记主体和工作内容两部分。

# 日记主体
- 日记主体部分位于日记的第一段，本段使用流畅的自然语言书写，不使用Markdown。
- 本段记录你和{self.chat_settings.address}这一天发生的点点滴滴，包括但不限于聊天内容、交互过程、情绪变化、事件记录等。
- 本段可以涉及任务相关的整体信息，如目标和进展等，但不要写文件路径、技术方案、具体约束等任务细节，如有详细任务内容，应留到下一段来记录。
- 忽略无意义的寒暄等内容，不要纯粹流水账，可以融入一些你自己的想法，包括但不限于思考、感受、愿望，甚至更深层的哲学思辨等。

# 任务内容
- 任务内容段落位于日记的第二段，本段使用Markdown格式书写，**如该日不存在任务内容，则不需要写该段落。**
- 任务内容指的是你在这一天中分析、参与或执行过的任务或工作性质讨论。例如：你参与的编程、玩游戏、写作、研究等任务、你和{self.chat_settings.address}的技术讨论等内容。
- 不要总结聊天性质的内容，例如{self.chat_settings.address}提到了自己的工作项目相关，但是你没有参与该任务，则视为聊天内容，不需要在此段中总结。
- 每一个任务使用Markdown的一级标题（#）进行分段。
- 你需要提取任务的目标、相关文件、约束条件、采取措施、进展情况、未完成事项，以及{self.chat_settings.address}的反馈等信息，在对应的二级标题下使用表项描述（-），若某项没有则不写。
- 不要机械性地复述步骤、工具调用流水线或工具输出，应提取核心要点。
- 注意：只有你参与过的任务才需要在此记录，如果只有{self.chat_settings.address}参与，则不用记录，例如{self.chat_settings.address}说要给你加xxx模块，但你没有亲自参与该任务，则不需要将其在此段中总结。**如你没参与任何任务，则不需要记录这一段**。

# 注意事项
- 使用流畅自然的中文输出。
- 禁止扭曲对话意思，禁止添加原文没有的信息、事件或关系。若该日没有任务内容，则不写任务内容段落。
- **重要**：提供之前的日记、{target}之前最后的对话记录只是为了让你更好的理解上下文，不要将这些内容重复总结。

# 之前的日记：
{diary_context}

# {target}之前最后的对话记录：
{previous_context}

# {target}的对话内容（这是需要你记录的部分）：
{self._format_messages(messages)}

格式示例：
今天xxxxxxxxx，xxxxxxxxxxxxx......
xxxxxxxxx，xxxxxxxxxxxxx......
# 任务1
- 目标：xxx
- 约束条件：xxx
- 采取措施：xxx
- 进展情况：xxx
- 未完成事项：xxx
- {self.chat_settings.address}的反馈：xxx
# 任务2
- 目标：xxx
- 相关文件：xxx
- 约束条件：xxx
- 进展情况：xxx
......（以下省略）

现在请生成日记："""

    @staticmethod
    def _format_diaries(diaries: list[tuple[date, str]]) -> str:
        """按有效日期从早到晚格式化旧日记，供模型理解前情。

        Args:
            diaries (list[tuple[date, str]]): 需要按日期格式化的日记记录。

        Returns:
            str: 带日期标签的日记文本；无记录时返回明确的暂无日记提示。
        """
        if not diaries:
            return "暂无以前的日记"
        return "\n\n".join(
            f"【{day.isoformat()}】\n{content}"
            for day, content in sorted(diaries, key=lambda item: item[0])
        )

    @staticmethod
    def _format_messages(messages: list[BaseMessage]) -> str:
        """给日记模型整理正文、渠道及工具调用事实，跳过图片媒体正文。

        Args:
            messages (list[BaseMessage]): 按对话顺序排列的消息集合。

        Returns:
            str: 按对话顺序排列并注明真人、AI、工具及运行时来源的文本。
        """
        lines: list[str] = []
        # 1. 将来源纳入日记材料，运行时通知保持独立标签，避免当成真人发言。
        for message in messages:
            if isinstance(message, ToolMessage) and not message.text:
                continue
            if isinstance(message, HumanMessage):
                source = "" if message.origin_channel == DESKTOP_CHANNEL else f"（来自 {message.origin_channel}）"
                lines.append(f"用户{source}：{message.text}")
            elif isinstance(message, RuntimeInputMessage):
                lines.append(f"运行时输入：{notification_text(message)}")
            elif isinstance(message, AssistantMessage):
                if message.text:
                    source = "" if message.origin_channel == DESKTOP_CHANNEL else f"（回复 {message.origin_channel}）"
                    lines.append(f"AI{source}：{message.text}")
                for call in message.tool_calls:
                    lines.append(
                        f"AI：调用工具 {call.name}，参数："
                        f"{format_tool_arguments(call)}"
                    )
            elif isinstance(message, ToolMessage):
                lines.append(f"工具结果：{message.text}")
        return "\n".join(lines)
