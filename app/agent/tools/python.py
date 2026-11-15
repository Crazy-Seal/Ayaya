"""把已审批的模型代码交给应用原生 Python REPL。"""

import base64

from pydantic import BaseModel, ConfigDict, Field, StrictStr

from app.agent.state.messages import ContentPart, ImageContent, TextContent
from app.agent.tools.core.base import BaseTool, ToolContext, ToolResult
from app.agent.tools.core.schema import tool_parameters_schema
from app.repl.contracts import PreparedReplCapability, ReplCallContext, ReplText
from app.repl.repl_service import ReplService
from app.schemas.message_types import MediaKind
from app.schemas.tool_types import PYTHON_TOOL_NAME


class PythonArguments(BaseModel):
    """要求模型提供执行目的及代码，供既有审批展示和绑定。"""

    model_config = ConfigDict(extra="forbid")
    intent: StrictStr = Field(min_length=1, description="说明本次 Python 执行的目的。")
    code: StrictStr = Field(min_length=1, description="在持久命名空间中执行的 Python 代码。")


class PythonTool(BaseTool):
    """调用应用共享 REPL，把其有序输出转换成普通工具内容。"""

    name = PYTHON_TOOL_NAME
    description = ("在 Python REPL 中执行 Python 代码。用 print 输出文字，"
                   "用 display_image(PIL图片或PNG/JPEG bytes) 输出图片；"
                   "变量、函数和导入等资源会在多次调用间保留，若需要清空请使用 python_reset 工具。"
                   "环境为本应用的固定 Python 环境，单次执行最多120秒，超时会停止执行。")
    parameters_schema = tool_parameters_schema(PythonArguments)
    compressible = True

    def __init__(self, service: ReplService, capabilities: tuple[PreparedReplCapability, ...]) -> None:
        """借用应用 REPL 和本会话已准备好的插件声明。

        Args:
            service (ReplService): 应用持有的唯一执行组件。
            capabilities (tuple[PreparedReplCapability, ...]): 本会话启用的插件能力。
        """
        self._service = service
        self._capabilities = capabilities

    async def execute(self, args: dict, context: ToolContext) -> ToolResult:
        """执行代码并保留文字图片顺序，交给现有保存链路。

        Args:
            args (dict): 已审批的 intent 和 code。
            context (ToolContext): Pipeline 提供的会话和调用来源。

        Returns:
            ToolResult: 普通文字、工具图片及实际错误。
        """
        try:
            options = PythonArguments.model_validate(args)
            result = await self._service.execute(options.code, ReplCallContext(
                context.session_id, context.origin_run_id, context.origin_tool_call_id, self._capabilities))
        except (ValueError, RuntimeError) as exc:
            return ToolResult.success(f"Python 执行失败：{exc}")
        content: list[ContentPart] = []
        for output in result.outputs:
            if isinstance(output, ReplText):
                if content and isinstance(content[-1], TextContent):
                    content[-1] = TextContent(content[-1].text + output.text)
                else:
                    content.append(TextContent(output.text))
            else:
                url = f"data:{output.mime_type};base64,{base64.b64encode(output.data).decode('ascii')}"
                content.append(ImageContent(url, media_kind=MediaKind.TOOL))
        if result.error:
            content.append(TextContent(result.error))
        if not content:
            content.append(TextContent("Python 执行完成，无显式输出"))
        return ToolResult(tuple(content))
