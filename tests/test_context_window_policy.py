from app.agent.context._projection import build_model_window
from app.agent.state import AgentState


def test_model_projection_no_longer_uses_fixed_human_window() -> None:
    """验证模型消息不再按固定真人输入数截取历史。"""
    state = AgentState.create_new("session")
    for index in range(25):
        state.add_user_message(f"human-{index}")
        state.add_assistant_message(f"assistant-{index}")

    messages = build_model_window(state)

    assert sum(1 for message in messages if message.get("role") == "user") == 25
