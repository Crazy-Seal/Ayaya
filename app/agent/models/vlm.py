"""基于同一张截图，在一次 VLM 请求中定位有序目标数组。"""

import json
import logging
import os
import re
from typing import Any

from app.agent.models.llm_client import LLMClient, LLMConfig
from app.agent.state.messages import SystemMessage, HumanMessage, ImageContent, TextContent

logger = logging.getLogger(__name__)

VLM_DEFAULT_MODEL = "qwen3-vl-plus"
VLM_DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"

_LOCATE_SYSTEM_PROMPT = (
    "你是一个PC屏幕视觉分析助手。你的任务是根据用户的文字描述，"
    "在同一张截图中一次定位 targets 数组中的全部目标。"
    "所有坐标都必须按图片宽高分别归一化到0～1000，边界框格式为[x1,y1,x2,y2]。"
    "返回格式必须是 {\"bboxes_2d\": [[x1,y1,x2,y2], ...]}，"
    "框的数量及顺序必须与 targets 严格一致，不能遗漏、合并或交换目标。"
    "所有目标必须在当前截图中已存在；不能预测后续操作后的新界面。"
    "找不到的目标在对应位置返回null，不得猜测坐标。不要输出Markdown或其他内容。"
)


def _vlm_config() -> tuple[str, str, str]:
    """读取截图定位模型的环境配置，缺少凭据时立即拒绝调用。

    Returns:
        tuple[str, str, str]: API 凭据、服务地址和模型名称。

    Raises:
        RuntimeError: 未配置 VLM_API_KEY。
    """
    api_key = os.getenv("VLM_API_KEY")
    if not api_key:
        raise RuntimeError("缺少VLM_API_KEY环境变量。")
    base_url = os.getenv("VLM_BASE_URL", VLM_DEFAULT_BASE_URL).strip()
    model = os.getenv("VLM_MODEL", VLM_DEFAULT_MODEL).strip()
    return api_key.strip(), base_url, model


def extract_bboxes(content: Any, expected_count: int) -> list[list[int]]:
    """解析全部定位框，任一目标缺失、数量不符或框无效时拒绝整个结果。

    Args:
        content (Any): VLM 返回的 JSON 文本或文本内容块。
        expected_count (int): 请求中的目标数量，返回数组必须逐项对应。

    Returns:
        list[list[int]]: 按目标顺序排列的完整 xyxy 整数框数组。

    Raises:
        ValueError: JSON 无效、数量不符、任一目标未找到或坐标框无效。
    """
    if isinstance(content, list):
        content = "".join(
            part.get("text", "") if isinstance(part, dict) else str(part) for part in content
        )
    text = str(content or "").strip()
    if not text:
        raise ValueError("视觉模型未返回可解析内容。")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"{[\s\S]*}", text)
        if not match:
            raise ValueError(f"模型返回不是JSON: {text[:200]}")
        parsed = json.loads(match.group(0))

    boxes = parsed.get("bboxes_2d") if isinstance(parsed, dict) else None
    if not isinstance(boxes, list) or len(boxes) != expected_count:
        raise ValueError("VLM 返回的 bboxes_2d 数量与目标数量不一致。")
    result = []
    # 1. 全部框都合法才交给工具，不能把部分定位成功误当作整批定位成功。
    for index, bbox in enumerate(boxes, start=1):
        if not isinstance(bbox, list) or len(bbox) != 4:
            raise ValueError(f"第 {index} 个目标未找到或坐标框无效。")
        if any(isinstance(value, bool) for value in bbox):
            raise ValueError(f"第 {index} 个目标的坐标不能是布尔值。")
        try:
            x1, y1, x2, y2 = [int(float(value)) for value in bbox]
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"第 {index} 个目标的坐标不是有限数字。") from exc
        if x2 <= x1 or y2 <= y1:
            raise ValueError(f"第 {index} 个目标的坐标框边界无效。")
        result.append([x1, y1, x2, y2])
    return result


class VLMService:
    """视觉模型服务。无状态，按需读取 VLM_* 环境变量。"""

    async def locate_many(self, intents: list[str], image_data_url: str) -> list[list[int]]:
        """一次请求定位同一张截图上的全部目标，不按目标循环调用模型。

        Args:
            intents (list[str]): 按动作顺序排列的非空目标描述。
            image_data_url (str): 完整 data URL（data:image/...;base64,...）

        Returns:
            list[list[int]]: 与输入目标一一对应的全部定位框。

        Raises:
            ValueError: 目标为空或模型未返回完整有效的定位结果。
            RuntimeError: 缺少定位模型配置。
        """
        if not intents or any(not isinstance(intent, str) or not intent.strip() for intent in intents):
            raise ValueError("VLM 定位需要至少一个非空目标。")
        api_key, base_url, model = _vlm_config()
        # 1. 保留原 SDK 默认超时与重试，只统一模型调用接口。
        client = LLMClient(LLMConfig(model=model, api_key=api_key, base_url=base_url,
                                     temperature=0, timeout=600, max_retries=2))
        try:
            response = await client.ainvoke([
                SystemMessage(text=_LOCATE_SYSTEM_PROMPT),
                HumanMessage(content=(ImageContent(image_data_url), TextContent(json.dumps(
                    {"targets": [intent.strip() for intent in intents]}, ensure_ascii=False,
                )))),
            ])
            return extract_bboxes(response.text, len(intents))
        finally:
            await client.close()
