"""声明屏幕能力与每次模型请求前的可选环境画面。"""

from pathlib import Path
import asyncio
import base64

from pydantic import BaseModel, ConfigDict, Field

from app.agent.plugins.base import BasePlugin, HookContext, PluginContext, PluginHook
from app.agent.state import ImageContent, TemporaryContextItem
from app.agent.utils.domain.images import save_image_to_disk
from app.repl.repl_api import ReplCapability
from app.schemas.message_types import MediaKind

from .repl.ayaya_computer_use import capture

PLUGIN_NAME = "computer_use"
GUIDE_KEY = f"{PLUGIN_NAME}.guide"
SCREEN_KEY = f"{PLUGIN_NAME}.screen"


class ComputerUseConfig(BaseModel):
    """让用户选择是否在每次主模型请求前提供当前主屏幕。"""

    model_config = ConfigDict(extra="forbid")
    inject_screen_state: bool = Field(default=False, title="注入当前屏幕", description="每次模型请求前提供主屏幕画面。")


class ComputerUsePlugin(BasePlugin):
    """预装 cua，并通过公开临时背景入口说明能力及提供环境截图。"""

    name = PLUGIN_NAME
    description = "通过 Python 中的 cua 观察窗口、控件树和画面，并操作 Windows。"
    config_model = ComputerUseConfig

    def __init__(self, *, context: PluginContext, inject_screen_state: bool = False) -> None:
        """保存本会话环境注入开关。

        Args:
            context (PluginContext): 宿主提供的配置快照和存储依赖。
            inject_screen_state (bool): 是否在模型请求前刷新主屏幕图片。
        """
        self._inject_screen_state = inject_screen_state

    @classmethod
    def resolve_repl_capabilities(cls, config: dict) -> tuple[ReplCapability, ...]:
        """声明 runner 自动创建的 cua，环境注入参数由后端钩子使用。

        Args:
            config (dict): 已校验的插件参数。

        Returns:
            tuple[ReplCapability, ...]: 模块位置及固定能力暴露名。
        """
        return (ReplCapability(Path(__file__).parent / "repl", "ayaya_computer_use.api", "cua", {}),)

    @property
    def hooks(self) -> list[PluginHook]:
        """在每次主模型请求前提供能力入口。

        Returns:
            list[PluginHook]: 模型请求前的钩子。
        """
        return [PluginHook.BEFORE_LLM]

    async def execute(self, context: HookContext) -> None:
        """向当前模型请求提供简短的能力使用入口。

        Args:
            context (HookContext): 本次模型请求的状态及临时背景接口。
        """
        context.agent_state.set_temporary_context(GUIDE_KEY, TemporaryContextItem(
            title="屏幕操作", text="Python REPL 已预装 cua；首次使用请执行 cua.help() 阅读观察、操作与停止说明。",
        ))
        # 1. 先移除上一请求的环境画面，捕获失败时由插件管理器记录错误。
        context.agent_state.remove_temporary_context(SCREEN_KEY)
        if self._inject_screen_state:
            image = await asyncio.to_thread(self._environment_image)
            context.agent_state.set_temporary_context(SCREEN_KEY, TemporaryContextItem(
                title="当前桌面", text="当前主屏幕画面，桌面坐标操作请显式调用 cua.get_screenshot() 取得映射。",
                images=(image,),
            ))

    @staticmethod
    def _environment_image() -> ImageContent:
        """捕获当前主屏幕并沿通用图片保存入口写入环境目录。

        Returns:
            ImageContent: 已保存到环境目录的文件引用图片。

        Raises:
            OSError: 捕获或保存失败，调用方已移除旧画面。
        """
        image = capture.capture_desktop()
        url = f"data:image/png;base64,{base64.b64encode(image.image).decode('ascii')}"
        reference = save_image_to_disk(url, category=MediaKind.ENVIRONMENT)
        if reference is None:
            raise OSError("环境图片保存失败")
        return ImageContent(media_kind=MediaKind.ENVIRONMENT, storage_ref=reference)
