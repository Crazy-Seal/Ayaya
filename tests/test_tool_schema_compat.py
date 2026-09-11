from copy import deepcopy

from app.agent.tools._schema import normalize_tool_schema_for_model
from app.agent.tools.base import BaseTool, ToolContext, ToolResult


class _NullableSchemaTool(BaseTool):
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
        return ToolResult.success("ok")


def test_openai_tool_normalizes_nullable_schema_recursively() -> None:
    parameters = _NullableSchemaTool().to_openai_tool()["function"]["parameters"]

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
    original = deepcopy(_NullableSchemaTool.parameters_schema)

    _NullableSchemaTool().to_openai_tool()

    assert _NullableSchemaTool.parameters_schema == original


def test_normalizer_preserves_required_nullable_semantics() -> None:
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
