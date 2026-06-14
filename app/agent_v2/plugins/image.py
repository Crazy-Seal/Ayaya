"""图片描述插件。

ON_INVOKE 时若最新用户消息含图片，启动后台 VLM 描述任务并把 key 记入
state.extra["image_task_key"]；await 与落库由 MemoryPlugin 在 BEFORE_RESPONSE 统一处理。
镜像旧 my_agent.py 的图片任务启动逻辑。
"""

import asyncio
import logging
import uuid

from app.agent_v2.context import BasePlugin, PluginHook, HookContext
from app.agent_v2.utils.context_window import _is_real_human
from app.agent_v2.utils.image_description import generate_multiple_image_descriptions
from app.agent_v2.utils.image_utils import has_image_content, set_image_task
from app.agent_v2.utils.text_utils import extract_text

logger = logging.getLogger(__name__)


class ImagePlugin(BasePlugin):
    name = "image"
    version = "1.0.0"
    priority = 50

    @property
    def hooks(self) -> list[PluginHook]:
        return [PluginHook.ON_INVOKE]

    async def execute(self, context: HookContext) -> HookContext:
        state = context.agent_state

        # 找最新一条真实用户消息
        target = None
        for msg in reversed(state.messages):
            if _is_real_human(msg):
                target = msg
                break
        if target is None:
            return context

        content = target.get("content")
        if not has_image_content(content):
            return context

        images = [
            part["image_url"]["url"]
            for part in content
            if isinstance(part, dict) and part.get("type") == "image_url" and part.get("image_url")
        ]
        if not images:
            return context

        text = extract_text(content)
        key = uuid.uuid4().hex
        task = asyncio.create_task(generate_multiple_image_descriptions(images, text, 200))
        set_image_task(key, task)
        state.extra["image_task_key"] = key
        logger.info("[ImagePlugin] 启动图片描述任务: %s (%d 张)", key, len(images))
        return context
