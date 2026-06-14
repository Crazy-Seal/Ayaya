"""access_the_internet 工具 - 通过 Tavily 进行互联网检索"""

import os

from app.agent_v2.context import BaseTool, ToolContext, ToolResult
from app.agent_v2.utils.log import log_tool_call_result, shorten_for_log


def _load_tavily_api_key() -> str | None:
    """读取 Tavily API Key：从 TAVILY_API_KEY 读取。"""
    key = (os.getenv("TAVILY_API_KEY") or "").strip()
    return key or None


def _format_search_output(data) -> str:
    """把搜索结果整理成紧凑可读文本。"""
    if isinstance(data, str):
        text = data.strip()
        return text if text else "未检索到相关互联网信息。"

    lines: list[str] = []
    if isinstance(data, dict):
        answer = str(data.get("answer") or "").strip()
        if answer:
            lines.append(f"综合结论: {shorten_for_log(answer, max_len=1200)}")
        results = data.get("results") or []
    elif isinstance(data, list):
        results = data
    else:
        results = []

    for idx, item in enumerate(results[:5], start=1):
        if isinstance(item, dict):
            title = str(item.get("title") or "无标题").strip()
            url = str(item.get("url") or "").strip()
            content = str(item.get("content") or item.get("snippet") or "").strip()
            content = shorten_for_log(content.replace("\n", " "), max_len=400)
        else:
            title = f"结果 {idx}"
            url = ""
            content = shorten_for_log(str(item), max_len=400)

        lines.append(f"{idx}. {title}")
        if url:
            lines.append(f"   链接: {url}")
        if content:
            lines.append(f"   摘要: {content}")

    if not lines:
        return "未检索到相关互联网信息。"
    return "互联网检索结果:\n" + "\n".join(lines)


class AccessTheInternetTool(BaseTool):
    name = "access_the_internet"
    description = (
        "访问互联网，搜索你想要的信息。当用户提及你不知道的信息，或你想上网查询时使用。\n"
        "Args:\n"
        "    query: 你的搜索问题或关键词等。"
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "你的搜索问题或关键词等。"},
        },
        "required": ["query"],
    }

    async def execute(self, args: dict, context: ToolContext) -> ToolResult:
        query = args.get("query", "")
        result = await self._search(query)
        await log_tool_call_result(self.name, args, result)
        if isinstance(result, str) and result.startswith("错误:"):
            return ToolResult(content=result)
        return ToolResult.success(result)

    async def _search(self, query: str) -> str:
        if not query or not query.strip():
            return "错误: query不能为空。"

        api_key = _load_tavily_api_key()
        if not api_key:
            return "错误: 未找到 Tavily API Key。请设置环境变量 TAVILY_API_KEY。"

        # 仅在当前进程设置环境变量，供 langchain-tavily 底层客户端读取。
        os.environ["TAVILY_API_KEY"] = api_key

        try:
            from langchain_tavily import TavilySearch

            search_tool = TavilySearch(
                max_results=5,
                search_depth="advanced",
                include_answer=True,
            )
            result = await search_tool.ainvoke({"query": query.strip()})
            return _format_search_output(result)
        except ModuleNotFoundError:
            return "错误: 缺少 langchain-tavily 依赖，请先安装: pip install langchain-tavily"
        except Exception as e:
            return f"错误: Tavily检索失败: {e}"
