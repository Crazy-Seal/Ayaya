"""编码子 Agent。

取代旧的 LangGraph subgraph_for_coding：v2 的 Agent 本身就是自洽的 LLM↔工具循环，
因此编码子图就是一个「带编码工具 + 动态计划提示词」的嵌套 Agent。

由 plan_and_coding 工具在后台调用，一次性执行（不持久化、不串入主对话）。
"""

import logging
import os

from app.agent_v2.agent import Agent, AgentConfig
from app.agent_v2.context import BasePlugin, PluginHook, HookContext
from app.agent_v2.state import AgentState
from app.agent_v2.utils.domain.text import extract_text
from app.agent_v2.subagents.todo_manager import TodoManager

logger = logging.getLogger(__name__)

CODING_DB_PATH = "memory/sqlite/agent_v2_coding.sqlite3"

CODING_TOOLS = ["run_ps", "read_file", "write_file", "edit_file", "delete_file", "update_plan"]

_CODING_SYSTEM_PROMPT = (
    "你是调用工具执行编程任务的编程专家。"
    "conda环境和工作目录都已经为你准备好，你可以直接调用工具执行命令和生成文件。"
    "每完成一条编码计划，必须调用工具更新编码计划列表的状态。"
    "若代码运行出错或缺少依赖，可根据错误提示修改代码或安装依赖后重试，直到成功为止。"
    "回答用户时，如执行成功直接回复运行结果，如需要，输出程序或项目的运行方法；如执行失败回复错误原因。"
    "回复禁止多于50字，禁止输出大块代码"
    "**重要事项：你必须根据以下的编码计划列表和用户的命令，调用工具逐步完成编程任务，"
    "执行任务时必须调用工具，并生成可执行的代码文件。**"
)


def _require_env(name: str) -> str:
    value = (os.getenv(name) or "").strip()
    if not value:
        raise RuntimeError(f"Environment variable not found: {name}")
    return value


class CodingPromptPlugin(BasePlugin):
    """BEFORE_LLM 时从 state.extra['todo_items'] 重建编码计划视图，注入提示词。

    复用 memory_context 通道（pipeline 会把它拼到 system_prompt 之后），
    使 update_plan 改动计划后下一轮提示词即时刷新。
    """

    name = "coding_prompt"
    version = "1.0.0"
    priority = 10

    @property
    def hooks(self) -> list[PluginHook]:
        return [PluginHook.BEFORE_LLM]

    async def execute(self, context: HookContext) -> HookContext:
        state = context.agent_state
        todo_items = state.extra.get("todo_items", [])
        todo_view = TodoManager().update(todo_items)
        state.memory_context = f"[编码计划列表]\n{todo_view}"
        return context


async def run_coding_subagent(command: str, todo_items: list[dict], session_id: str) -> str:
    """运行编码子 Agent，返回最后一条 AI 文本结果。"""
    config = AgentConfig(
        session_id=f"{session_id}:coding",
        model_name=_require_env("CODING_MODEL"),
        api_key=_require_env("CODING_API_KEY"),
        base_url=_require_env("CODING_BASE_URL"),
        temperature=float(_require_env("CODING_TEMPERATURE")),
        system_prompt=_CODING_SYSTEM_PROMPT,
        tools=CODING_TOOLS,
        plugins=["context_window"],
    )
    agent = Agent(config, db_path=CODING_DB_PATH)
    await agent.initialize()
    await agent.plugin_manager.register(CodingPromptPlugin(), agent)

    # 一次性执行：构造全新状态，注入待办与命令，直接驱动 pipeline（不持久化）
    state = AgentState.create_new(config.session_id)
    state.extra["todo_items"] = list(todo_items)
    state.add_user_message(command)

    try:
        async for _event in agent.pipeline.execute(state):
            pass
    finally:
        await agent.close()

    # 取最后一条 assistant 文本作为输出
    for msg in reversed(state.messages):
        if msg.get("role") == "assistant":
            text = extract_text(msg.get("content")).strip()
            if text:
                return text
    return "[未返回内容]"
