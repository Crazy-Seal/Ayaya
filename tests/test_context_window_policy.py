from app.agent.context_strategy import ContextStrategyConfig, ContextStrategyManager
from app.agent.state import AgentState


def test_model_projection_no_longer_uses_fixed_human_window() -> None:
    manager = ContextStrategyManager(ContextStrategyConfig(compression_trigger_tokens_k=100))
    state = AgentState.create_new("session")
    for index in range(25):
        state.add_user_message(f"human-{index}")
        state.add_assistant_message(f"assistant-{index}")

    messages = manager.build_model_window(state)

    assert sum(1 for message in messages if message.get("role") == "user") == 25
