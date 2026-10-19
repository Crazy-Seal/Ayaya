from tests.model_capture import encoded_tool
from copy import deepcopy

from pydantic import BaseModel, Field

from app.agent.tools.core.schema import normalize_tool_schema_for_model, tool_parameters_schema
from app.agent.tools.core.base import BaseTool, ToolContext, ToolResult


class _NullableSchemaTool(BaseTool):
    """提供用例指定的工具响应、中断或异常，验证执行管道的结果保存与恢复。"""
    name = "nullable_schema"
    description = "验证模型工具 Schema 兼容转换"
    parameters_schema = {
        "type": "object",
        "properties": {
            "seed": {
                "anyOf": [
                    {"type": "string"},
                    {"type": "null"},
                ],
                "default": None,
                "title": "Seed",
            },
            "payload": {
                "type": "object",
                "properties": {
                    "target": {
                        "anyOf": [
                            {"type": "string", "enum": ["enemy", "self"]},
                            {"type": "null"},
                        ],
                        "default": None,
                        "description": "可选目标",
                    }
                },
            },
            "mode": {
                "type": ["string", "null"],
                "default": "auto",
            },
        },
    }

    async def execute(self, args: dict, context: ToolContext) -> ToolResult:
        """管道或插件管理器调用时执行本用例预设的结果、状态修改或异常分支。

        Args:
            args (dict): 当前工具调用或被包装函数的参数。
            context (ToolContext): 执行管道提供的工具运行上下文。

        Returns:
            ToolResult: 供外层用例断言使用的预设结果。
        """
        return ToolResult.success("ok")


def test_openai_tool_normalizes_nullable_schema_recursively() -> None:
    """验证工具 Schema 递归规范化可空联合。"""
    parameters = encoded_tool(_NullableSchemaTool())["function"]["parameters"]

    assert parameters["properties"]["seed"] == {
        "type": "string",
        "nullable": True,
        "title": "Seed",
    }
    assert parameters["properties"]["payload"]["properties"]["target"] == {
        "type": "string",
        "enum": ["enemy", "self"],
        "nullable": True,
        "description": "可选目标",
    }
    assert parameters["properties"]["mode"] == {
        "type": "string",
        "nullable": True,
        "default": "auto",
    }


def test_openai_tool_does_not_mutate_registered_schema() -> None:
    """验证Schema 规范化不修改注册表原定义。"""
    original = deepcopy(_NullableSchemaTool.parameters_schema)

    encoded_tool(_NullableSchemaTool())

    assert _NullableSchemaTool.parameters_schema == original


def test_normalizer_preserves_required_nullable_semantics() -> None:
    """验证可空规范化仍保留字段必填语义。"""
    normalized = normalize_tool_schema_for_model({
        "type": "object",
        "properties": {
            "value": {
                "anyOf": [
                    {"type": "integer"},
                    {"type": "null"},
                ],
            }
        },
        "required": ["value"],
    })

    assert normalized["required"] == ["value"]
    assert normalized["properties"]["value"] == {
        "type": "integer",
        "nullable": True,
    }


def test_normalizer_does_not_narrow_real_multi_type_union() -> None:
    """验证真实多类型联合不会被错误收窄。"""
    schema = {
        "type": "object",
        "properties": {
            "value": {
                "anyOf": [
                    {"type": "string"},
                    {"type": "integer"},
                ],
            }
        },
    }

    assert normalize_tool_schema_for_model(schema) == schema


def test_parameter_schema_omits_type_docs_and_preserves_field_descriptions() -> None:
    """验证根模型和复用模型的文档不进入参数 Schema，显式字段说明及约束保持不变。"""
    class NestedOptions(BaseModel):
        """内部嵌套参数实现说明。"""

        count: int = Field(default=5, ge=0, le=10, description="最多返回条数。")

    class Arguments(BaseModel):
        """内部参数校验说明。"""

        options: NestedOptions = Field(description="本次查询选项。")

    schema = tool_parameters_schema(Arguments)
    assert "description" not in schema
    assert "description" not in schema["$defs"]["NestedOptions"]
    assert schema["properties"]["options"]["description"] == "本次查询选项。"
    assert schema["$defs"]["NestedOptions"]["properties"]["count"] == {
        "type": "integer", "title": "Count", "default": 5,
        "minimum": 0, "maximum": 10, "description": "最多返回条数。",
    }
    assert schema["required"] == ["options"]
    # 1. 工具定义使用独立 Schema，不影响参数模型本身保留给维护者的文档。
    assert Arguments.model_json_schema()["description"] == Arguments.__doc__
    assert Arguments.model_json_schema()["$defs"]["NestedOptions"]["description"] == NestedOptions.__doc__
