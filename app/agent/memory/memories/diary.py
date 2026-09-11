"""每日记忆：从协议日志生成和检索日记。"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from app.agent.memory.config import MemoryConfig
from app.agent.memory.store.chat_history_store import ChatHistoryStore
from app.agent.memory.store.diary_sqlite_store import DiarySqliteStore
from app.agent.state.messages import is_system_injected_user_message
from app.agent.models.llm_client import LLMClient, LLMConfig
from app.agent.utils.domain.text import extract_text
from app.schemas.chat_settings import ChatSettings


logger = logging.getLogger(__name__)


class DiaryMemory:
    """从协议日志生成并检索每日记忆。"""

    def __init__(
        self,
        session_id: str,
        config: MemoryConfig,
        chat_settings: ChatSettings,
        chat_history_store: ChatHistoryStore,
    ) -> None:
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

    async def check_and_generate(
        self, today: date, *, max_history_id: int | None = None
    ) -> None:
        target = await self.chat_history_store.get_last_chat_date(
            self.session_id, exclude_today=today, max_history_id=max_history_id
        )
        if target is None or await self.has_diary(target):
            return
        messages = await self.chat_history_store.get_messages_by_date(
            self.session_id, target, max_history_id=max_history_id
        )
        if messages:
            await self._generate_diary(
                target,
                messages,
                max_history_id=max_history_id,
            )

    async def add(self, date_obj: date, content: str) -> int:
        return await self.store.add(self.session_id, date_obj, content)

    async def search(self, date_obj: date) -> str | None:
        return await self.store.get(self.session_id, date_obj)

    async def has_diary(self, date_obj: date) -> bool:
        return await self.store.exists(self.session_id, date_obj)

    async def search_diary(self, start: date, end: date) -> list[tuple[date, str]]:
        return await self.store.get_range(self.session_id, start, end)

    async def get_context(self, today: date) -> str:
        diaries = await self.store.get_recent_before_date(
            self.session_id, today, n=self.config.recent_diaries_count
        )
        return "\n\n".join(
            f"【{day.isoformat()}】\n{content}" for day, content in reversed(diaries)
        )

    async def _generate_diary(
        self,
        target_date: date,
        messages: list[dict[str, Any]],
        *,
        max_history_id: int | None = None,
    ) -> None:
        try:
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
            response = await self.llm.ainvoke([{"role": "user", "content": prompt}])
            await self.add(target_date, response.content)
        except Exception:
            logger.exception("[DiaryMemory] 生成日记失败: date=%s", target_date)

    def _build_diary_prompt(
        self,
        messages: list[dict[str, Any]],
        target_date: date,
        *,
        previous_diaries: list[tuple[date, str]],
        previous_messages: list[dict[str, Any]],
    ) -> str:
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
- 日记主体部分位于日记的第一段，本段使用流畅的自然语言书写。
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

# 注意事项
- 使用流畅自然的中文输出。
- 禁止扭曲对话意思，禁止添加原文没有的信息、事件或关系。若该日没有任务内容，则不写任务内容段落。
- **重要**：提供之前的日记、{target}之前最后的对话记录只是为了让你更好的理解上下文，不要将这些内容重复总结。

之前的日记：
{diary_context}

{target}之前最后的对话记录：
{previous_context}

{target}的对话内容（这是需要你记录的部分）：
{self._format_messages(messages)}

请生成日记："""

    @staticmethod
    def _format_diaries(diaries: list[tuple[date, str]]) -> str:
        if not diaries:
            return "暂无以前的日记"
        return "\n\n".join(
            f"【{day.isoformat()}】\n{content}"
            for day, content in sorted(diaries, key=lambda item: item[0])
        )

    @staticmethod
    def _format_messages(messages: list[dict[str, Any]]) -> str:
        import json
        lines: list[str] = []
        for message in messages:
            if is_system_injected_user_message(message):
                continue
            role = message.get("role")
            content = extract_text(message.get("content"))
            if role == "user":
                lines.append(f"用户：{content}")
            elif role == "assistant":
                if content:
                    lines.append(f"AI：{content}")
                for call in message.get("tool_calls") or []:
                    function = call.get("function") or {}
                    lines.append(
                        f"AI：调用工具 {function.get('name', '未知工具')}，参数："
                        f"{function.get('arguments', '{}')}"
                    )
            elif role == "tool":
                lines.append(f"工具结果：{content}")
        return "\n".join(lines)
