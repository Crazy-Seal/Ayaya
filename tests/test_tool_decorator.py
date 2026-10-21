import asyncio
import inspect
from typing import Annotated, Literal, Optional

import pytest
from pydantic import BaseModel, Field

from app.agent.tools.core.base import ToolContext, ToolResult
from app.agent.tools.core.decorator import tool
from tests.model_capture import encoded_tool


def test_tool_schema_includes_signature_defaults() -> None:
    """验证自动工具 Schema 包含签名默认值。"""
    @tool(description="验证参数默认值。")
    async def sample_tool(
        required_text: Annotated[str, "required text"],
        mode: Annotated[Literal["fast", "slow"], "mode"] = "fast",
        count: Annotated[int, "count"] = 3,
        enabled: Annotated[bool, "enabled"] = False,
        note: Annotated[str, "note"] = "",
    ) -> str:
        """声明带默认值和受限制参数的测试工具，供检查自动 Schema。

        该定义仅用于外层用例：验证自动工具 Schema 包含签名默认值。

        Args:
            required_text (str): required text
            mode (Literal['fast', 'slow']): mode
            count (int): count
            enabled (bool): enabled
            note (str): note

        Returns:
            str: 供外层用例断言使用的预设结果。
        """
        return required_text

    schema = sample_tool.parameters_schema

    assert schema["required"] == ["required_text"]
    assert "default" not in schema["properties"]["required_text"]
    assert schema["properties"]["mode"]["default"] == "fast"
    assert schema["properties"]["count"]["default"] == 3
    assert schema["properties"]["enabled"]["default"] is False
    assert schema["properties"]["note"]["default"] == ""


def test_explicit_parameters_schema_is_not_modified() -> None:
    """验证显式工具 Schema 不被装饰器修改。"""
    explicit_schema = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "name",
            },
        },
    }

    @tool(description="验证显式参数定义。", parameters_schema=explicit_schema)
    async def explicit_schema_tool(name: Annotated[str, "name"] = "demo") -> str:
        """声明显式 Schema 的测试工具，供检查原定义不会被装饰器修改。

        该定义仅用于外层用例：验证显式工具 Schema 不被装饰器修改。

        Args:
            name (str): name

        Returns:
            str: 供外层用例断言使用的预设结果。
        """
        return name

    assert explicit_schema_tool.parameters_schema is explicit_schema
    assert "default" not in explicit_schema_tool.parameters_schema["properties"]["name"]


@pytest.mark.parametrize("explicit_schema", [False, True])
@pytest.mark.parametrize(
    ("provided", "expected"),
    [
        ({}, {"count": 3, "enabled": True, "note": "默认正文", "optional_text": "默认备注"}),
        (
            {"count": 0, "enabled": False, "note": "", "optional_text": None},
            {"count": 0, "enabled": False, "note": "", "optional_text": None},
        ),
    ],
)
def test_execute_distinguishes_omitted_and_explicit_arguments(explicit_schema, provided, expected) -> None:
    """验证两种 Schema 定义方式都保留函数默认值，并原样转交显式提供的值。

    Args:
        explicit_schema (bool): 是否使用显式参数 Schema。
        provided (dict): 模型实际提供的可选参数；空字典表示全部省略。
        expected (dict): 原函数应收到的参数，包括默认值或显式的零、False、空串和 None。
    """
    observed = {}
    context = ToolContext(
        session_id="defaults-test", state=None,
    )

    async def sample_tool(
        required_text: Annotated[str, "必填正文。"],
        count: Annotated[int, "数量。"] = 3,
        enabled: Annotated[bool, "是否启用。"] = True,
        note: Annotated[str, "附加正文。"] = "默认正文",
        optional_text: Annotated[str | None, "可为空的备注。"] = "默认备注",
        context: ToolContext | None = None,
    ) -> str:
        """记录装饰器实际传入的值，验证默认值与运行时上下文注入。

        Args:
            required_text (str): 模型提供的必填正文。
            count (int): 数量，省略时为三。
            enabled (bool): 是否启用，省略时启用。
            note (str): 正文，允许显式传入空字符串。
            optional_text (str | None): 备注，允许显式传入 None。
            context (ToolContext | None): 执行管道注入的上下文。

        Returns:
            str: 模型提供的必填正文。
        """
        observed.update(count=count, enabled=enabled, note=note, optional_text=optional_text, context=context)
        return required_text

    schema = {
        "type": "object",
        "properties": {name: {} for name in ("required_text", "count", "enabled", "note", "optional_text")},
        "required": ["required_text"],
    } if explicit_schema else None
    wrapped = tool(sample_tool, description="验证参数执行默认值。", parameters_schema=schema)
    result = asyncio.run(wrapped().execute({"required_text": "正文", **provided}, context))

    assert result.text == "正文"
    assert observed == {**expected, "context": context}


def test_execute_does_not_replace_missing_required_argument_with_none() -> None:
    """验证省略必填参数时由函数调用报错，不向原函数伪造 None 参数。"""
    @tool(description="验证必填参数。")
    async def sample_tool(value: Annotated[str, "必填正文。"]) -> str:
        """返回输入正文，用于检查缺少必填参数时原函数不会执行。

        Args:
            value (str): 不允许省略的正文。

        Returns:
            str: 原样返回的正文。
        """
        return value

    with pytest.raises(TypeError, match="value"):
        asyncio.run(sample_tool().execute({}, None))


def test_annotated_field_constraints_reach_model_schema() -> None:
    """验证 Annotated 中的 Field 约束、文字说明和函数默认值一起发送给模型。"""
    @tool(description="等待指定秒数。")
    async def sample_tool(
        seconds: Annotated[int, "等待秒数。", Field(ge=0, le=60)] = 3,
    ) -> str:
        """提供带等待范围的测试定义，不执行真实等待。

        Args:
            seconds (int): 允许 0～60 的等待秒数。

        Returns:
            str: 本次等待秒数的文本。
        """
        return str(seconds)

    schema = encoded_tool(sample_tool())["function"]["parameters"]
    assert schema["properties"]["seconds"] == {
        "type": "integer", "description": "等待秒数。",
        "minimum": 0, "maximum": 60, "default": 3,
    }
    assert "seconds" not in schema.get("required", [])


@pytest.mark.parametrize("nullable_type", [int | None, Optional[int]])
@pytest.mark.parametrize("constrained", [False, True])
def test_nullable_parameters_preserve_nullability_and_required_status(nullable_type, constrained) -> None:
    """验证两种可空注解均保留 null 分支，是否必填仍由函数默认值决定。

    Args:
        nullable_type (Any): 使用竖线或 Optional 声明的可空整数类型。
        constrained (bool): 是否额外通过 Field 声明数值范围。
    """
    annotation = (
        Annotated[nullable_type, "等待次数。", Field(ge=0, le=60)]
        if constrained else Annotated[nullable_type, "等待次数。"]
    )

    @tool(description="验证可空参数。")
    async def sample_tool(required_count: annotation, optional_count: annotation = None) -> str:
        """声明必填可空参数和可省略参数，不执行实际等待。

        Args:
            required_count (int | None): 必须提供，允许为空的次数。
            optional_count (int | None): 可省略，默认值为空的次数。

        Returns:
            str: 测试工具的固定结果。
        """
        return "完成"

    raw = sample_tool.parameters_schema
    sent = encoded_tool(sample_tool())["function"]["parameters"]
    concrete = {"type": "integer", **({"minimum": 0, "maximum": 60} if constrained else {})}
    assert raw["required"] == sent["required"] == ["required_count"]
    for name in ("required_count", "optional_count"):
        assert raw["properties"][name]["anyOf"] == [concrete, {"type": "null"}]
        assert sent["properties"][name] == {**concrete, "nullable": True, "description": "等待次数。"}
    assert "default" not in raw["properties"]["required_count"]
    assert raw["properties"]["optional_count"]["default"] is None


def test_nested_nullable_item_reaches_model_schema() -> None:
    """验证列表元素中的可空类型也发送给模型，不把整个列表误标为可空。"""
    @tool(description="接收可含空值的数值列表。")
    async def sample_tool(values: Annotated[list[int | None], "数值列表。"]) -> str:
        """提供嵌套可空元素的测试定义。

        Args:
            values (list[int | None]): 可包含空值的整数列表。

        Returns:
            str: 测试工具的固定结果。
        """
        return "完成"

    values = encoded_tool(sample_tool())["function"]["parameters"]["properties"]["values"]
    assert values == {
        "type": "array", "description": "数值列表。",
        "items": {"type": "integer", "nullable": True},
    }


def test_multi_type_nullable_union_is_not_narrowed() -> None:
    """验证多个具体类型与 None 联合时，模型仍收到所有允许的类型。"""
    @tool(description="接收整数、文字或空值。")
    async def sample_tool(value: Annotated[int | str | None, "输入内容。"]) -> str:
        """提供多类型联合的测试定义。

        Args:
            value (int | str | None): 模型提供的整数、文字或空值。

        Returns:
            str: 测试工具的固定结果。
        """
        return "完成"

    value = encoded_tool(sample_tool())["function"]["parameters"]["properties"]["value"]
    assert value == {
        "anyOf": [{"type": "integer"}, {"type": "string"}, {"type": "null"}],
        "description": "输入内容。",
    }


def test_model_description_is_independent_of_docstring_and_context() -> None:
    """验证模型只收到显式说明和参数定义，维护文档保留，运行上下文仍能注入。"""
    @tool(description="搜索用户指定的内容。")
    async def sample_tool(
        query: Annotated[str, "搜索关键词。"],
        context: ToolContext,
    ) -> ToolResult:
        """由 Pipeline 注入上下文，按当前会话处理查询。

        Args:
            query (str): 待查询文本，由模型提供。
            context (ToolContext): 执行时注入的内部依赖，不属于模型参数。

        Returns:
            ToolResult: 包含查询文本的结果。
        """
        assert context is expected_context
        return ToolResult.success(query)

    expected_context = ToolContext(
        session_id="description-test", state=None,
    )
    function = encoded_tool(sample_tool())["function"]
    assert function == {
        "name": "sample_tool",
        "description": "搜索用户指定的内容。",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "搜索关键词。"}},
            "required": ["query"],
        },
    }
    original_doc = inspect.getdoc(sample_tool._tool_func)
    assert original_doc and "context (ToolContext)" in original_doc
    # 1. 维护者修改实现文档后，发送给模型的定义必须保持不变。
    sample_tool._tool_func.__doc__ = "更新后的内部实现说明。"
    assert encoded_tool(sample_tool())["function"] == function
    # 2. 模型只提交 query，由执行管道传入的 context 仍交给原函数使用。
    result = asyncio.run(sample_tool().execute({"query": "用户偏好"}, expected_context))
    assert result.text == "用户偏好"


def test_nested_model_docstring_is_not_used_as_parameter_description() -> None:
    """验证嵌套模型的维护文档不会发送，Annotated 和 Field 中的参数说明仍保留。"""
    class SearchOptions(BaseModel):
        """仅供维护者阅读的内部查询配置说明。"""

        limit: int = Field(default=5, ge=1, le=10, description="最多返回条数。")

    @tool(description="按指定条数搜索。")
    async def sample_tool(options: Annotated[SearchOptions, "查询选项。"]) -> str:
        """提供嵌套参数定义，不执行实际查询。

        Args:
            options (SearchOptions): 待查询的配置对象。

        Returns:
            str: 未执行查询时的空文本。
        """
        return ""

    options = encoded_tool(sample_tool())["function"]["parameters"]["properties"]["options"]
    assert options == {
        "type": "object",
        "description": "查询选项。",
        "properties": {
            "limit": {
                "type": "integer", "default": 5, "minimum": 1, "maximum": 10,
                "description": "最多返回条数。",
            },
        },
    }


@pytest.mark.parametrize("description", ["", " \n\t", None, 123])
def test_tool_rejects_invalid_explicit_description(description: object) -> None:
    """验证工具定义阶段拒绝无效说明，不回退使用维护文档。

    Args:
        description (object): 空文本、空白文本或非字符串说明。
    """
    with pytest.raises(ValueError, match="description"):
        tool(description=description)


def test_tool_requires_explicit_description() -> None:
    """验证即使函数有维护文档，省略模型说明仍会在定义阶段失败。"""
    async def sample_tool() -> str:
        """这段维护文档不能自动成为模型工具说明。

        Returns:
            str: 测试工具的固定结果。
        """
        return ""

    with pytest.raises(TypeError, match="description"):
        tool(sample_tool)
