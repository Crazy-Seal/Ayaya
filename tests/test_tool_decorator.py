from typing import Annotated, Literal

from app.agent.tools.decorator import tool


def test_tool_schema_includes_signature_defaults() -> None:
    """验证自动工具 Schema 包含签名默认值。"""
    @tool
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

    @tool(parameters_schema=explicit_schema)
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
