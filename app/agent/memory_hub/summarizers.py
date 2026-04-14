from functools import lru_cache

from langchain_core.messages import AnyMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from app.agent.memory_hub.config import MemoryConfig
from app.agent.memory_hub.constants import (
    LATER_HUMAN_MESSAGES_FOR_SUMMARY,
    PREVIOUS_HUMAN_MESSAGES_FOR_SUMMARY,
)
from app.agent.memory_hub.text_utils import (
    build_summary_source,
    extract_text,
    split_context,
    split_summary_items,
)


@lru_cache
def get_summary_model(memory_config: MemoryConfig) -> ChatOpenAI:
    """构建用于记忆提取与融合的总结模型。"""
    return ChatOpenAI(
        model=memory_config.model_name,
        base_url=memory_config.openai_base_url,
        api_key=SecretStr(memory_config.openai_api_key),
        temperature=0,
    )


async def merge_long_memory_text_async(memory_config: MemoryConfig, existing_text: str, new_text: str) -> str:
    """融合两条相似长期记忆。"""
    if existing_text == new_text:
        return existing_text

    prompt = (
        "请把两条相似的长期记忆合并为一条更完整、去重后的记忆。"
        "保持事实准确，保留所有有效信息，不要新增原文没有的信息，只输出最终一条记忆。"
    )
    model = get_summary_model(memory_config)
    response = await model.ainvoke([
        SystemMessage(content=prompt),
        HumanMessage(content=f"已有记忆：{existing_text}\n新记忆：{new_text}"),
    ])
    merged_text = extract_text(response.content).strip()
    return merged_text if merged_text else f"{existing_text}；{new_text}"


async def summarize_episodic_memory_items_async(
    memory_config: MemoryConfig,
    messages: list[AnyMessage],
    short_memory: str,
) -> list[str]:
    """提取情景记忆条目，不负责写库。"""
    previous_tail_messages, later_messages = split_context(
        messages,
        LATER_HUMAN_MESSAGES_FOR_SUMMARY,
        PREVIOUS_HUMAN_MESSAGES_FOR_SUMMARY,
    )
    previous_tail_source = build_summary_source(previous_tail_messages)
    later_source = build_summary_source(later_messages)
    if not previous_tail_source and not later_source:
        return []

    prompt = (
        """你是记忆提取专家。请基于已有短期记忆、前情提要和要总结的对话，提取可长期存储的关键事实记忆，用自然流畅的语言记录。\n\n
身份说明：
- "主人"是使用AI的真人用户
- "助手"是AI助手（不是真人）

提取规则：
1. 用自然的中文描述，像写日记一样记录要点
2. 示例格式：
   主人常在晚上与AI互动，称呼AI为日和；喜欢听AI唱歌
   主人说自己生日是9月25日，希望AI记住
   主人最近在学Python，问了很多编程问题
   AI承诺帮主人提醒明天的会议
3. 不相关的要点需要分成不同的记忆条目，每条只记录一个事实或事件
4. 每条记忆15-50字，保留关键细节
5. 忽略无意义的闲聊（如"嗯"、"好的"、"知道了"）
6. 可以记忆以下内容，重要程度从高到低：
   - 生日、重大事件、用户核心偏好、用户特征
   - 习惯、经历、明确表态
   - 普通话题、临时想法
7. 注意：提供短期记忆和前情提要只是为了让你理解上下文，你只需要总结要总结的对话中的新信息
8. 禁止使用“现在”“最近”“目前”“正在”等表示现在进行时的时间词，以保持时间中立性
9. 可以提取0-10条记忆，返回多条记忆时使用换行分隔，不要输出额外解释。没有记忆要总结就返回None"""
    )
    model = get_summary_model(memory_config)
    response = await model.ainvoke([
        SystemMessage(content=prompt),
        HumanMessage(
            content=(
                f"已有短期记忆：\n{short_memory}\n\n"
                f"前情提要：\n{previous_tail_source}\n\n"
                f"要总结的对话：\n{later_source}"
            )
        ),
    ])
    summary_text = extract_text(response.content)
    if summary_text in ["None", "none", "NONE", ""]:
        return []
    return split_summary_items(summary_text)


async def summarize_short_memory_async(
    memory_config: MemoryConfig,
    messages: list[AnyMessage],
    previous_short_memory: str,
) -> str:
    """生成新的短期记忆段落。"""
    previous_tail_messages, later_messages = split_context(
        messages,
        LATER_HUMAN_MESSAGES_FOR_SUMMARY,
        PREVIOUS_HUMAN_MESSAGES_FOR_SUMMARY,
    )
    previous_tail_source = build_summary_source(previous_tail_messages)
    later_source = build_summary_source(later_messages)
    if not previous_tail_source and not later_source:
        return previous_short_memory

    prompt = (
        "你是短期记忆归纳助手。请基于已有短期记忆、之前的对话结尾与之后的对话，"
        f"将这些所有的记忆更新为一段连续、自然的新短期记忆（{memory_config.short_term_min_chars}-{memory_config.short_term_max_chars}字）。"
        "要求保留事件时间、事件摘要等重要细节，删除无意义闲聊，"
        "并确保和之前记忆衔接自然，不要重复堆砌。"
        "用中文输出，只输出一段话。"
        "如果字数过多，可以适量精简或删去时间较早的事件。"
        "禁止改变段落意思，禁止添加之前没有的信息或事件关系。"
    )
    model = get_summary_model(memory_config)
    response = await model.ainvoke([
        SystemMessage(content=prompt),
        HumanMessage(
            content=(
                f"已有短期记忆：\n{previous_short_memory}\n\n"
                f"之前的对话结尾（之前5轮）：\n{previous_tail_source}\n\n"
                f"之后的对话（最后10轮）：\n{later_source}"
            )
        ),
    ])
    short_memory_text = extract_text(response.content).strip()
    return short_memory_text if short_memory_text else previous_short_memory


async def summarize_semantic_items_async(
    memory_config: MemoryConfig,
    messages: list[AnyMessage],
) -> list[str]:
    """提取语义记忆条目（偏好、事实、稳定属性）。"""
    source = build_summary_source(messages)
    if not source:
        return []

    prompt = (
        "你是语义记忆提取器。请从对话中提取适合长期保存的稳定信息："
        "用户偏好、客观事实、永久约定、稳定背景。"
        "每条10-60字，按行输出，最多5条。"
        "如果没有可提取内容，输出None。"
    )
    model = get_summary_model(memory_config)
    response = await model.ainvoke([
        SystemMessage(content=prompt),
        HumanMessage(content=f"对话内容：\n{source}"),
    ])
    semantic_text = extract_text(response.content)
    if semantic_text in ["None", "none", "NONE", ""]:
        return []
    return split_summary_items(semantic_text)

