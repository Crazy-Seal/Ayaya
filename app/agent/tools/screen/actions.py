"""校验屏幕工具的整批动作，生成审批和桌面执行共用的数据。"""

from typing import Annotated, Any, TypeVar

from app.schemas.desktop import ScreenOperation, ScrollDirection
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr, field_validator, model_validator


BATCH_SCREEN_INSTRUCTIONS = (
    "actions 按数组顺序执行，你可以传入一个或多个动作。调用本工具前必须了解当前屏幕情况，如不了解，先使用截图工具查看。"
    "一次传入多个动作时，必须百分之百确定：除最后一个动作外，前面的操作不会导致"
    "后续目标消失、被遮挡或位置发生变化。例如弹出确认框、进入其他网页、打开新的应用 UI、展开菜单、"
    "滚动页面或输入触发联想列表等，都可能使后续坐标失效，产生不可预测的后果。"
    "如无法百分之百确定前面的动作不会导致以上后果，就只能传入一个动作，收到操作后的新截图后再决定下一步。"
    "每轮工具调用中只能使用一次本工具。整批执行完毕并收到截图后，再规划下一批。"
)


def normalize_bbox(bbox: object) -> list[int]:
    """校验归一化的 xyxy 框，不接受浮点数、布尔值或颠倒的边界。

    Args:
        bbox (object): 模型提供的四个整数，允许 Pipeline 固定为不可变元组。

    Returns:
        list[int]: 范围在 0～1000 且满足 x1 < x2、y1 < y2 的边界框。

    Raises:
        ValueError: 类型、长度、范围或边界顺序无效。
    """
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        raise ValueError("bbox 必须是包含四个整数的数组。")
    if any(type(value) is not int for value in bbox):
        raise ValueError("bbox 坐标必须是整数。")
    if any(not 0 <= value <= 1000 for value in bbox):
        raise ValueError("bbox 坐标必须在 0 到 1000 之间。")
    x1, y1, x2, y2 = bbox
    if x2 <= x1 or y2 <= y1:
        raise ValueError("bbox 必须满足 x1 < x2 且 y1 < y2。")
    return [x1, y1, x2, y2]


class ScreenActionArguments(BaseModel):
    """保存单个待定位动作；整批校验成功后才允许截图定位或产生桌面输入。"""

    model_config = ConfigDict(extra="forbid")
    target: StrictStr = Field(min_length=1, description="要操作的目标元素描述，例如页面上方的搜索框。")
    operation: ScreenOperation = Field(default=ScreenOperation.DOUBLE, description="操作方式：click 单击、double 双击、right 右键、scroll 滚动。")
    text: StrictStr = Field(default="", description="鼠标操作后输入的文字；空字符串表示不输入。")
    press_enter: StrictBool = Field(default=True, description="输入文字后是否按回车；没有输入文字时不按回车。")
    wait_seconds: int = Field(default=3, ge=0, le=60, strict=True, description="该动作完成后的等待秒数，范围 0～60。")
    scroll_direction: ScrollDirection = Field(default=ScrollDirection.DOWN, description="滚动方向。scroll 操作只使用 down 或 up；其他操作忽略此字段。")

    @field_validator("target")
    @classmethod
    def normalize_target(cls, value: str) -> str:
        """在动作校验时去掉目标两侧空白，不允许只包含空白的描述。

        Args:
            value (str): 模型提供的目标文字。

        Returns:
            str: 可供定位和审批展示的目标描述。

        Raises:
            ValueError: 去掉空白后没有目标内容。
        """
        value = value.strip()
        if not value:
            raise ValueError("缺少要操作的元素描述。")
        return value

    @model_validator(mode="after")
    def validate_scroll_direction(self) -> "ScreenActionArguments":
        """校验鼠标操作与滚动方向的组合。

        Returns:
            ScreenActionArguments: 方向可用于本次动作的参数对象。

        Raises:
            ValueError: 滚动操作没有指定向上或向下。
        """
        if self.operation == ScreenOperation.SCROLL and self.scroll_direction == ScrollDirection.NONE:
            raise ValueError("滚动操作的 scroll_direction 必须是 down 或 up。")
        return self


class LocatedScreenActionArguments(ScreenActionArguments):
    """保存已定位的动作，审批材料和桌面操作均从此对象生成。"""

    bbox: list[Annotated[int, Field(ge=0, le=1000, strict=True)]] = Field(
        min_length=4, max_length=4, description="目标边界框 [x1, y1, x2, y2]，按屏幕宽高归一化到 0～1000。",
    )

    @field_validator("bbox", mode="before")
    @classmethod
    def validate_bbox(cls, value: object) -> list[int]:
        """在类型转换前校验坐标，避免把浮点数或布尔值强制转成整数。

        Args:
            value (object): 模型提供或 VLM 预测后确认的坐标框。

        Returns:
            list[int]: 可用于计算屏幕中心点的边界框。

        Raises:
            ValueError: 坐标框不符合四整数和范围约束。
        """
        return normalize_bbox(value)

    def execution_data(self) -> dict[str, Any]:
        """生成可保存的审批材料及桌面比例坐标。

        Returns:
            dict[str, Any]: 保留目标、原始框和全部操作配置的独立字典。
        """
        x1, y1, x2, y2 = self.bbox
        return {
            **self.model_dump(),
            "coordinates": {"bbox": list(self.bbox), "x_ratio": (x1 + x2) / 2000, "y_ratio": (y1 + y2) / 2000},
        }


ActionArguments = TypeVar("ActionArguments", bound=ScreenActionArguments)


def validate_actions(actions: object, model: type[ActionArguments]) -> list[ActionArguments]:
    """整批校验动作，任意一个无效时拒绝开始整个批次。

    Args:
        actions (object): 模型提供的数组，Pipeline 可以将其保存为不可变元组。
        model (type[ActionArguments]): 需要或不需要模型提供坐标框的动作类型。

    Returns:
        list[ActionArguments]: 按原顺序校验并补齐各自默认值的动作对象。

    Raises:
        ValueError: 数组为空、不是数组，或任意一个动作无效。
    """
    if not isinstance(actions, (list, tuple)) or not actions:
        raise ValueError("actions 必须是至少包含一个动作的数组。")
    result = []
    # 1. 全部动作都校验成功才返回，后面的无效动作不能导致前面的动作已经执行。
    for index, action in enumerate(actions, start=1):
        try:
            result.append(model.model_validate(action))
        except ValueError as exc:
            raise ValueError(f"第 {index} 个动作无效：{exc}") from exc
    return result
