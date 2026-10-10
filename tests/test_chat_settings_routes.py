import pytest

from app.agent.context import ContextStrategyConfig
from app.routes.chat_settings import add_chat_settings, update_chat_settings
from app.schemas.chat_settings import ChatSettings


def _settings(policy: str) -> ChatSettings:
    """构造当前用例需要的最小会话设置。

    Args:
        policy (str): 本用例指定的权限或压缩策略。

    Returns:
        ChatSettings: 供外层用例断言使用的预设结果。
    """
    return ChatSettings(
        session_id="normalized-route",
        model_name="model",
        openai_api_key="key",
        openai_base_url="http://127.0.0.1:1/v1",
        temperature=0.1,
        system_prompt="prompt",
        tool_permissions={"mcp:filesystem:read": policy},
        context_strategy=ContextStrategyConfig(compression_trigger_tokens_k=100),
        mcp={"servers": {"filesystem": {"enabled": True}}},
    )


class _NormalizingService:
    """提供当前用例所需的 _NormalizingService 测试替身，执行记录仅供外层断言使用。"""
    async def add_chat_settings(self, _incoming: ChatSettings) -> ChatSettings:
        """模拟设置新增并返回规范化对象，供验证路由响应使用保存结果。

        Args:
            _incoming (ChatSettings): 本次模拟模型调用收到的消息。

        Returns:
            ChatSettings: 供外层用例断言使用的预设结果。
        """
        return _settings("ask")

    async def update_chat_settings(self, _incoming: ChatSettings) -> ChatSettings:
        """模拟设置更新并返回规范化对象，供验证路由响应使用保存结果。

        Args:
            _incoming (ChatSettings): 本次模拟模型调用收到的消息。

        Returns:
            ChatSettings: 供外层用例断言使用的预设结果。
        """
        return _settings("ask")


@pytest.mark.asyncio
@pytest.mark.parametrize("handler", [add_chat_settings, update_chat_settings])
async def test_chat_settings_write_returns_normalized_saved_object(handler) -> None:
    """验证设置写入接口返回实际规范化并保存的对象。

    Args:
        handler (Any): 本次模拟工具执行的回调。
    """
    result = await handler(_settings("allow"), _NormalizingService())

    assert result.data.tool_permissions["mcp:filesystem:read"].value == "ask"
