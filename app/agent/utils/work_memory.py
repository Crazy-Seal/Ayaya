from langchain_core.messages import AnyMessage, HumanMessage


def slice_recent_messages_by_human(messages: list[AnyMessage], max_human_messages: int = 10) -> list[AnyMessage]:
    """控制工作记忆，从后往前数到第 max_human_messages 条 HumanMessage，并保留该条到结尾的所有消息。

    注意：截图消息（name 为 system_screenshot 或 system_screenshot_compressed）不计入人类消息计数。
    """
    # 延迟导入避免循环依赖
    from app.agent.state import is_screenshot_message

    human_count = 0
    start_index = 0

    for index in range(len(messages) - 1, -1, -1):
        msg = messages[index]
        # 只计数真正的用户消息，过滤截图消息
        if isinstance(msg, HumanMessage) and not is_screenshot_message(msg):
            human_count += 1
            if human_count == max_human_messages:
                start_index = index
                break

    return messages[start_index:]