from app.agent.state.messages import text_content
import asyncio
from datetime import date

from app.agent.context import ContextStrategyConfig
from app.agent.memory import manager as memory_manager_module
from app.agent.memory.config import MemoryConfig
from app.agent.memory.manager import MemoryManager
from app.agent.memory.memories import episodic as episodic_module
from app.agent.state.messages import HumanMessage
from app.schemas.chat_settings import AgentPluginSettings, ChatSettings


def _chat_settings(*, enable_diary=True, enable_episodic=True, enable_semantic=True):
    """构造当前 MCP 或记忆测试所需的会话配置。

    Args:
        enable_diary (Any): 本用例是否开启日记能力。
        enable_episodic (Any): 本用例是否开启情景记忆能力。
        enable_semantic (Any): 本用例是否开启语义记忆能力。
    """
    return ChatSettings(
        session_id="test-session",
        model_name="test-model",
        openai_api_key="test-key",
        openai_base_url="http://127.0.0.1:1/v1",
        temperature=0.1,
        system_prompt="test",
        context_strategy=ContextStrategyConfig(compression_trigger_tokens_k=100),
        agent_plugins={
            "memory": AgentPluginSettings(
                enabled=True,
                config={
                    "enable_diary": enable_diary,
                    "enable_episodic": enable_episodic,
                    "enable_semantic": enable_semantic,
                },
            )
        },
        skills=[],
    )


def _capture_episodic_llm_config(monkeypatch, config: MemoryConfig):
    """替换提取模型客户端并记录构造配置，供检查是否使用独立设置。

    Args:
        monkeypatch (pytest.MonkeyPatch): Pytest 提供的替换工具，用例结束后恢复环境变量和依赖。
        config (MemoryConfig): 调用方提供的配置。
    """
    captured = {}

    class FakeStore:
        """提供当前用例所需的 FakeStore 测试替身，执行记录仅供外层断言使用。"""
        def __init__(self, *args, **kwargs):
            """保存本用例的脚本输入和依赖，初始化测试对象的执行记录。

            Args:
                args (dict[str, Any]): 当前工具调用或被包装函数的参数。
                kwargs (dict[str, Any]): 传递给被调用函数的其他关键字参数。
            """
            pass

    class FakeLLMClient:
        """提供当前用例所需的 FakeLLMClient 测试替身，执行记录仅供外层断言使用。"""
        def __init__(self, llm_config):
            """保存本用例的脚本输入和依赖，初始化测试对象的执行记录。

            Args:
                llm_config (Any): 需要记录以验证独立提取模型的配置。
            """
            captured["config"] = llm_config

    monkeypatch.setattr(episodic_module, "EpisodicChromaStore", FakeStore)
    monkeypatch.setattr(episodic_module, "EpisodicSqliteStore", FakeStore)
    monkeypatch.setattr(episodic_module, "LLMClient", FakeLLMClient)
    episodic_module.EpisodicMemory("test-session", config, _chat_settings())
    return captured["config"]


def test_memory_config_reads_episodic_extraction_environment(monkeypatch) -> None:
    """验证记忆配置读取独立情景提取环境设置。

    Args:
        monkeypatch (pytest.MonkeyPatch): Pytest 提供的替换工具，用例结束后恢复环境变量和依赖。
    """
    monkeypatch.setenv("EPISODIC_EXTRACTION_MODEL", "gpt-test")
    monkeypatch.setenv("EPISODIC_EXTRACTION_BASE_URL", "https://memory.example/v1")
    monkeypatch.setenv("EPISODIC_EXTRACTION_API_KEY", "memory-key")

    config = MemoryConfig.from_env()

    assert config.episodic_extraction_model == "gpt-test"
    assert config.episodic_extraction_base_url == "https://memory.example/v1"
    assert config.episodic_extraction_api_key == "memory-key"


def test_episodic_memory_uses_independent_extraction_config(monkeypatch) -> None:
    """验证情景记忆使用独立提取模型配置。

    Args:
        monkeypatch (pytest.MonkeyPatch): Pytest 提供的替换工具，用例结束后恢复环境变量和依赖。
    """
    llm_config = _capture_episodic_llm_config(
        monkeypatch,
        MemoryConfig(
            episodic_extraction_model="gpt-test",
            episodic_extraction_base_url="https://memory.example/v1",
            episodic_extraction_api_key="memory-key",
        ),
    )

    assert llm_config.model == "gpt-test"
    assert llm_config.base_url == "https://memory.example/v1"
    assert llm_config.api_key == "memory-key"


def test_episodic_memory_uses_only_explicit_model_without_chat_fallback(monkeypatch) -> None:
    """验证情景提取只使用显式模型而不回退聊天模型。

    Args:
        monkeypatch (pytest.MonkeyPatch): Pytest 提供的替换工具，用例结束后恢复环境变量和依赖。
    """
    llm_config = _capture_episodic_llm_config(
        monkeypatch,
        MemoryConfig(episodic_extraction_model="gpt-test"),
    )

    assert llm_config.model == "gpt-test"
    assert llm_config.base_url == ""
    assert llm_config.api_key == ""


def test_episodic_memory_does_not_fall_back_to_chat_model_config(monkeypatch) -> None:
    """验证情景记忆缺少配置时不回退聊天模型。

    Args:
        monkeypatch (pytest.MonkeyPatch): Pytest 提供的替换工具，用例结束后恢复环境变量和依赖。
    """
    llm_config = _capture_episodic_llm_config(monkeypatch, MemoryConfig())

    assert llm_config.model == ""
    assert llm_config.base_url == ""
    assert llm_config.api_key == ""


def test_memory_manager_skips_disabled_memory_subsystems(monkeypatch) -> None:
    """验证禁用的记忆子系统不装配。

    Args:
        monkeypatch (pytest.MonkeyPatch): Pytest 提供的替换工具，用例结束后恢复环境变量和依赖。
    """
    constructed: list[str] = []

    class FakeChatHistoryStore:
        """提供当前用例所需的 FakeChatHistoryStore 测试替身，执行记录仅供外层断言使用。

        该定义仅用于外层用例：验证禁用的记忆子系统不装配。
        """
        def __init__(self, *args, **kwargs):
            """保存本用例的脚本输入和依赖，初始化测试对象的执行记录。

            该定义仅用于外层用例：验证禁用的记忆子系统不装配。

            Args:
                args (dict[str, Any]): 当前工具调用或被包装函数的参数。
                kwargs (dict[str, Any]): 传递给被调用函数的其他关键字参数。
            """
            constructed.append("chat_history")

    class DisabledSubsystem:
        """提供当前用例所需的 DisabledSubsystem 测试替身，执行记录仅供外层断言使用。

        该定义仅用于外层用例：验证禁用的记忆子系统不装配。
        """
        def __init__(self, *args, **kwargs):
            """保存本用例的脚本输入和依赖，初始化测试对象的执行记录。

            该定义仅用于外层用例：验证禁用的记忆子系统不装配。

            Args:
                args (dict[str, Any]): 当前工具调用或被包装函数的参数。
                kwargs (dict[str, Any]): 传递给被调用函数的其他关键字参数。
            """
            raise AssertionError("disabled memory subsystem should not initialize")

    monkeypatch.setattr(memory_manager_module, "ChatHistoryStore", FakeChatHistoryStore)
    monkeypatch.setattr(memory_manager_module, "DiaryMemory", DisabledSubsystem)
    monkeypatch.setattr(memory_manager_module, "EpisodicMemory", DisabledSubsystem)
    monkeypatch.setattr(memory_manager_module, "Mem0SemanticMemory", DisabledSubsystem)

    manager = MemoryManager(
        "test-session",
        MemoryConfig(),
        _chat_settings(enable_diary=False, enable_episodic=False, enable_semantic=False),
    )

    assert constructed == []
    assert manager.diary_memory is None
    assert manager.episodic_memory is None
    assert manager.semantic_memory is None


def test_memory_manager_uses_mem0_even_with_unrelated_backend_env(monkeypatch) -> None:
    """验证其他后端环境值不改变 Mem0 语义记忆选择。

    Args:
        monkeypatch (pytest.MonkeyPatch): Pytest 提供的替换工具，用例结束后恢复环境变量和依赖。
    """
    constructed: list[tuple[str, str]] = []

    class FakeChatHistoryStore:
        """提供当前用例所需的 FakeChatHistoryStore 测试替身，执行记录仅供外层断言使用。

        该定义仅用于外层用例：验证其他后端环境值不改变 Mem0 语义记忆选择。
        """
        def __init__(self, *args, **kwargs):
            """保存本用例的脚本输入和依赖，初始化测试对象的执行记录。

            该定义仅用于外层用例：验证其他后端环境值不改变 Mem0 语义记忆选择。

            Args:
                args (dict[str, Any]): 当前工具调用或被包装函数的参数。
                kwargs (dict[str, Any]): 传递给被调用函数的其他关键字参数。
            """
            pass

    class DisabledSubsystem:
        """提供当前用例所需的 DisabledSubsystem 测试替身，执行记录仅供外层断言使用。

        该定义仅用于外层用例：验证其他后端环境值不改变 Mem0 语义记忆选择。
        """
        def __init__(self, *args, **kwargs):
            """保存本用例的脚本输入和依赖，初始化测试对象的执行记录。

            该定义仅用于外层用例：验证其他后端环境值不改变 Mem0 语义记忆选择。

            Args:
                args (dict[str, Any]): 当前工具调用或被包装函数的参数。
                kwargs (dict[str, Any]): 传递给被调用函数的其他关键字参数。
            """
            raise AssertionError("disabled memory subsystem should not initialize")

    class FakeMem0SemanticMemory:
        """提供当前用例所需的 FakeMem0SemanticMemory 测试替身，执行记录仅供外层断言使用。

        该定义仅用于外层用例：验证其他后端环境值不改变 Mem0 语义记忆选择。
        """
        def __init__(self, session_id, config, chat_settings):
            """保存本用例的脚本输入和依赖，初始化测试对象的执行记录。

            该定义仅用于外层用例：验证其他后端环境值不改变 Mem0 语义记忆选择。

            Args:
                session_id (Any): 当前操作所属的会话编号。
                config (Any): 调用方提供的配置。
                chat_settings (Any): 当前会话的模型、角色及插件设置。
            """
            constructed.append((session_id, chat_settings.session_id))

    monkeypatch.setenv("SEMANTIC_BACKEND", "native")
    monkeypatch.setattr(memory_manager_module, "ChatHistoryStore", FakeChatHistoryStore)
    monkeypatch.setattr(memory_manager_module, "DiaryMemory", DisabledSubsystem)
    monkeypatch.setattr(memory_manager_module, "EpisodicMemory", DisabledSubsystem)
    monkeypatch.setattr(memory_manager_module, "Mem0SemanticMemory", FakeMem0SemanticMemory)

    manager = MemoryManager(
        "test-session",
        MemoryConfig.from_env(),
        _chat_settings(enable_diary=False, enable_episodic=False, enable_semantic=True),
    )

    assert constructed == [("test-session", "test-session")]
    assert isinstance(manager.semantic_memory, FakeMem0SemanticMemory)


def test_check_diary_is_noop_when_diary_disabled() -> None:
    """验证日记禁用后检查操作不访问日记对象。"""
    async def scenario() -> None:
        """由外层测试调用，完成“日记禁用后检查操作不访问日记对象”的异步步骤。"""
        manager = MemoryManager(
            "test-session",
            MemoryConfig(),
            _chat_settings(enable_diary=False, enable_episodic=False, enable_semantic=False),
        )
        await manager.check_diary()

    asyncio.run(scenario())


def test_memory_manager_check_diary_uses_protocol_history_cutoff(monkeypatch) -> None:
    """验证日记检查使用提交前协议日志边界。

    Args:
        monkeypatch (pytest.MonkeyPatch): Pytest 提供的替换工具，用例结束后恢复环境变量和依赖。
    """
    checks: list[tuple[date, int | None]] = []

    class FakeChatHistoryStore:
        """提供当前用例所需的 FakeChatHistoryStore 测试替身，执行记录仅供外层断言使用。

        该定义仅用于外层用例：验证日记检查使用提交前协议日志边界。
        """
        def __init__(self, *args, **kwargs):
            """保存本用例的脚本输入和依赖，初始化测试对象的执行记录。

            该定义仅用于外层用例：验证日记检查使用提交前协议日志边界。

            Args:
                args (dict[str, Any]): 当前工具调用或被包装函数的参数。
                kwargs (dict[str, Any]): 传递给被调用函数的其他关键字参数。
            """
            pass

    class FakeDiaryMemory:
        """提供当前用例所需的 FakeDiaryMemory 测试替身，执行记录仅供外层断言使用。

        该定义仅用于外层用例：验证日记检查使用提交前协议日志边界。
        """
        def __init__(self, *args, **kwargs):
            """保存本用例的脚本输入和依赖，初始化测试对象的执行记录。

            该定义仅用于外层用例：验证日记检查使用提交前协议日志边界。

            Args:
                args (dict[str, Any]): 当前工具调用或被包装函数的参数。
                kwargs (dict[str, Any]): 传递给被调用函数的其他关键字参数。
            """
            pass

        async def check_and_generate(
            self,
            effective_date: date,
            *,
            max_history_id: int | None = None,
        ) -> None:
            """记录日记检查日期和历史边界，避免调用真实生成模型。

            该定义仅用于外层用例：验证日记检查使用提交前协议日志边界。

            Args:
                effective_date (date): 按日记规则划分的有效日期，格式为 YYYY-MM-DD。
                max_history_id (int | None): 本次提取开始前捕获的协议日志最大编号；排除之后到达的消息。
            """
            checks.append((effective_date, max_history_id))

    monkeypatch.setattr(memory_manager_module, "ChatHistoryStore", FakeChatHistoryStore)
    monkeypatch.setattr(memory_manager_module, "DiaryMemory", FakeDiaryMemory)

    manager = MemoryManager(
        "test-session",
        MemoryConfig(),
        _chat_settings(enable_diary=True, enable_episodic=False, enable_semantic=False),
    )
    asyncio.run(manager.check_diary(max_history_id=17))

    assert checks and checks[0][1] == 17


def test_disabled_memory_subsystems_are_not_accessed(monkeypatch) -> None:
    """验证禁用的记忆子系统不会被访问。

    Args:
        monkeypatch (pytest.MonkeyPatch): Pytest 提供的替换工具，用例结束后恢复环境变量和依赖。
    """
    class FakeChatHistoryStore:
        """提供当前用例所需的 FakeChatHistoryStore 测试替身，执行记录仅供外层断言使用。

        该定义仅用于外层用例：验证禁用的记忆子系统不会被访问。
        """
        def __init__(self, *args, **kwargs):
            """保存本用例的脚本输入和依赖，初始化测试对象的执行记录。

            该定义仅用于外层用例：验证禁用的记忆子系统不会被访问。

            Args:
                args (dict[str, Any]): 当前工具调用或被包装函数的参数。
                kwargs (dict[str, Any]): 传递给被调用函数的其他关键字参数。
            """
            pass

    class DisabledSubsystem:
        """提供当前用例所需的 DisabledSubsystem 测试替身，执行记录仅供外层断言使用。

        该定义仅用于外层用例：验证禁用的记忆子系统不会被访问。
        """
        def __init__(self, *args, **kwargs):
            """保存本用例的脚本输入和依赖，初始化测试对象的执行记录。

            该定义仅用于外层用例：验证禁用的记忆子系统不会被访问。

            Args:
                args (dict[str, Any]): 当前工具调用或被包装函数的参数。
                kwargs (dict[str, Any]): 传递给被调用函数的其他关键字参数。
            """
            raise AssertionError("disabled memory subsystem should not initialize")

    monkeypatch.setattr(memory_manager_module, "ChatHistoryStore", FakeChatHistoryStore)
    monkeypatch.setattr(memory_manager_module, "DiaryMemory", DisabledSubsystem)
    monkeypatch.setattr(memory_manager_module, "EpisodicMemory", DisabledSubsystem)
    monkeypatch.setattr(memory_manager_module, "Mem0SemanticMemory", DisabledSubsystem)

    async def scenario() -> tuple[dict[str, int], str, str, str]:
        """由外层测试调用，完成“禁用的记忆子系统不会被访问”的异步步骤。

        Returns:
            tuple[dict[str, int], str, str, str]: 供外层用例断言使用的预设结果。
        """
        manager = MemoryManager(
            "test-session",
            MemoryConfig(),
            _chat_settings(enable_diary=False, enable_episodic=False, enable_semantic=False),
        )
        add_result = await manager.add(
            [HumanMessage(content=text_content("近期对话"))],
            enable_episodic=False,
            enable_semantic=False,
        )
        context = await manager.get_context("查询")
        search_result = await manager.search("查询", "all")
        diary_result = await manager.search_diary(date(2024, 1, 1), date(2024, 1, 2))
        return add_result, context, search_result, diary_result

    add_result, context, search_result, diary_result = asyncio.run(scenario())

    assert add_result == {"episodic": 0, "semantic": 0}
    assert context == ""
    assert search_result == "未找到相关记忆"
    assert diary_result == "日记记忆未启用"
