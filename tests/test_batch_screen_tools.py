"""验证屏幕动作数组整批校验、VLM 多框完整性及审批后执行的接口契约。"""

import asyncio
import json

import pytest

from app.agent.models.vlm import VLMService, extract_bboxes
from app.agent.tools.control_screen import control_screen
from app.agent.tools.control_screen_with_vlm import control_screen_with_vlm
from app.agent.tools.screen.adapter import screen_tool_result
from app.schemas.desktop import ScreenActionResult
from tests.model_capture import encoded_tool
from tests.test_backend_screen_tools import DesktopPort, context


@pytest.mark.parametrize("tool_class", [control_screen, control_screen_with_vlm])
@pytest.mark.parametrize("invalid", ["empty", "target", "wait", "boolean"])
def test_invalid_batch_is_rejected_before_desktop_or_vlm(tool_class, invalid, monkeypatch):
    """后面的动作无效时，整个数组都不能开始截图、定位或输入。

    Args:
        tool_class (type[BaseTool]): 直接定位或使用 VLM 的屏幕工具。
        invalid (str): 空数组、空白目标、越界等待或错误布尔类型。
        monkeypatch (pytest.MonkeyPatch): 禁止本用例访问真实桌面和 VLM。
    """
    class UnusedDesktop(DesktopPort):
        """无效数组不能调用桌面依赖。"""
        async def capture_screen(self, *, stop_requested=None):
            """若无效批次仍开始截图则立即令测试失败。

            Args:
                stop_requested (StopRequested | None): 原流程的停止检查。
            """
            raise AssertionError("无效批次不应截图")

        async def perform_actions(self, actions, *, stop_requested=None):
            """若无效批次仍执行动作则立即令测试失败。

            Args:
                actions (tuple[ScreenAction, ...]): 不应被交付的动作数组。
                stop_requested (StopRequested | None): 原流程的停止检查。
            """
            raise AssertionError("无效批次不应产生输入")

    async def unused_vlm(*_args):
        """若无效批次仍请求模型则立即令测试失败。

        Args:
            _args (tuple): 不应被交付的定位请求。
        """
        raise AssertionError("无效批次不应请求 VLM")

    monkeypatch.setattr(VLMService, "locate_many", unused_vlm)
    actions = [{"target": "第一个目标"}, {"target": "第二个目标"}]
    if tool_class is control_screen:
        for action in actions:
            action["bbox"] = [100, 100, 200, 200]
    if invalid == "empty":
        actions = []
    elif invalid == "target":
        actions[-1]["target"] = " "
    elif invalid == "wait":
        actions[-1]["wait_seconds"] = 61
    else:
        actions[-1]["press_enter"] = "false"
    result = asyncio.run(tool_class().execute({"actions": actions}, context(UnusedDesktop())))
    assert result.text.startswith("错误:") and result.prepared_action is None


@pytest.mark.parametrize("tool_class", [control_screen, control_screen_with_vlm])
def test_action_schema_exposes_defaults_without_internal_docs(tool_class):
    """模型看到动作数组、每项默认值与约束，而看不到维护文档或上下文对象。

    Args:
        tool_class (type[BaseTool]): 当前核验的屏幕工具类。
    """
    function = encoded_tool(tool_class())["function"]
    schema = function["parameters"]
    assert schema["required"] == ["actions"]
    assert schema["properties"]["actions"]["minItems"] == 1
    item = schema["properties"]["actions"]["items"]
    assert item["additionalProperties"] is False
    assert "description" not in item
    assert item["properties"]["operation"]["default"] == "double"
    assert item["properties"]["press_enter"]["default"] is True
    assert item["properties"]["wait_seconds"]["minimum"] == 0
    assert item["properties"]["wait_seconds"]["maximum"] == 60
    assert item["properties"]["wait_seconds"]["default"] == 3
    assert ("bbox" in item["required"]) == (tool_class is control_screen)
    assert "context" not in schema["properties"]
    assert "Pipeline" not in json.dumps(function, ensure_ascii=False)


@pytest.mark.parametrize("boxes", [
    [[1, 2, 3, 4]],
    [[1, 2, 3, 4], None],
    [[1, 2, 3, 4], [8, 8, 1, 1]],
    [[1, 2, 3, 4], [True, 0, 3, 4]],
    [[1, 2, 3, 4], [0, 0, float("inf"), 3]],
])
def test_vlm_rejects_incomplete_or_invalid_box_array(boxes):
    """模型不能用部分有效结果开始执行，任一缺失或无效框都使整个定位失败。

    Args:
        boxes (list): 数量不符、缺失目标、颠倒、布尔或非有限坐标组成的响应。
    """
    with pytest.raises(ValueError):
        extract_bboxes(json.dumps({"bboxes_2d": boxes}), 2)


@pytest.mark.parametrize("stop", [False, True])
def test_vlm_mismatch_or_stop_does_not_prepare_partial_batch(monkeypatch, stop):
    """数量不符或定位期间已停止时，不提交审批材料，也不执行部分动作。

    Args:
        monkeypatch (pytest.MonkeyPatch): 用固定定位结果替代网络请求。
        stop (bool): 是否在 VLM 返回时模拟本轮已停止。
    """
    stopped = False

    async def locate_many(_self, _targets, _image):
        """返回少一个框，或返回完整框但同时触发停止。

        Args:
            _self (VLMService): 当前测试替代的定位服务。
            _targets (list[str]): 待定位的两个目标。
            _image (str): 测试截图。

        Returns:
            list[list[int]]: 不完整结果或停止后的完整结果。
        """
        nonlocal stopped
        stopped = stop
        return [[1, 2, 3, 4]] * (2 if stop else 1)

    monkeypatch.setattr(VLMService, "locate_many", locate_many)
    desktop = DesktopPort()
    result = asyncio.run(control_screen_with_vlm().execute(
        {"actions": [{"target": "第一个目标"}, {"target": "第二个目标"}]},
        context(desktop, stop_requested=lambda: stopped),
    ))
    assert result.prepared_action is None and not desktop.actions
    assert ("停止" if stop else "数量") in result.text


def test_batch_failure_result_reports_progress_without_claiming_no_input():
    """已完成第一步而第二步不确定时，模型必须看到进度和禁止整批重放的说明。"""
    result = screen_tool_result(ScreenActionResult(
        "partial_or_unknown", error="第二步输入中断", completed_actions=1, total_actions=3,
    ))
    assert "1/3" in result.text and "第二步输入中断" in result.text
    assert "可能已部分执行" in result.text
    assert "后续动作未继续执行" in result.text
    assert "屏幕操作未执行" not in result.text
