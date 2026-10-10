"""工具定义内部使用的模型 JSON Schema 兼容转换。"""

from __future__ import annotations

from typing import Any


def normalize_tool_schema_for_model(schema: dict[str, Any]) -> dict[str, Any]:
    """返回适合模型 function calling 的独立 Schema 副本。

    部分兼容 OpenAI API 的网关会把工具转换为 Gemini FunctionDeclaration，
    无法接受仅通过 ``anyOf`` 或类型数组表达的可空字段。对于单一具体类型与
    ``null`` 的联合，这里保留具体类型；参数是否可选仍由父对象的
    ``required`` 决定。

    Args:
        schema (dict[str, Any]): 工具注册时保存的原始 JSON Schema。

    Returns:
        dict[str, Any]: 递归复制并收窄可空联合后的 Schema，不修改输入对象。
    """
    normalized = _normalize_node(schema)
    return normalized if isinstance(normalized, dict) else {}


def _normalize_node(node: Any) -> Any:
    """递归复制工具 Schema，逐层规范化可空联合，不修改注册表原定义。

    Args:
        node (Any): 当前递归处理的 JSON Schema 节点。

    Returns:
        Any: 经过规范化的独立节点，标量保持原值。
    """
    if isinstance(node, dict):
        normalized = {key: _normalize_node(value) for key, value in node.items()}
        return _collapse_nullable_union(normalized)
    if isinstance(node, list):
        return [_normalize_node(value) for value in node]
    return node


def _collapse_nullable_union(schema: dict[str, Any]) -> dict[str, Any]:
    """将唯一具体类型与 null 的联合改为 nullable 标记，保留真正的多类型联合。

    Args:
        schema (dict[str, Any]): 当前要检查或转换的 JSON Schema。

    Returns:
        dict[str, Any]: 供兼容模型网关使用的字段定义。
    """
    any_of = schema.get("anyOf")
    if isinstance(any_of, list):
        concrete = [branch for branch in any_of if not _is_null_schema(branch)]
        if len(concrete) == 1 and len(any_of) == 2 and isinstance(concrete[0], dict):
            schema = {
                **concrete[0],
                **{key: value for key, value in schema.items() if key != "anyOf"},
            }
            schema["nullable"] = True
            if schema.get("default") is None:
                schema.pop("default", None)

    schema_type = schema.get("type")
    if isinstance(schema_type, list):
        concrete_types = [value for value in schema_type if value != "null"]
        if len(concrete_types) == 1 and len(schema_type) == 2:
            schema["type"] = concrete_types[0]
            schema["nullable"] = True
            if schema.get("default") is None:
                schema.pop("default", None)
    return schema


def _is_null_schema(schema: Any) -> bool:
    """识别 Schema 分支是否只声明 null 类型。

    Args:
        schema (Any): 当前要检查或转换的 JSON Schema。

    Returns:
        bool: 节点为对象且 type 为 null 时为 True。
    """
    return isinstance(schema, dict) and schema.get("type") == "null"
