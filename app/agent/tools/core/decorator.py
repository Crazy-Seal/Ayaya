"""函数式工具装饰器 @tool。

把一个带类型注解的 async 函数，自动变成一个 BaseTool 子类：
- name        ← 函数名（或显式传入）
- description ← @tool(description=...) 中显式声明的模型说明；docstring 仅供维护者阅读
- parameters_schema ← 从签名 + 类型注解自动合成（结构）；每个参数的文字描述写在
                       Annotated[类型, "说明"] 里（必填，缺失则装饰时报错），可追加 Field 声明约束
- execute     ← 适配器：args dict → kwargs，注入 context（若声明），统一日志与结果包装

因为产出的是 BaseTool 子类，注册表、ToolManager 和执行管道使用统一工具接口。

名为 `context` 的参数被视为运行时注入的 ToolContext，不进 schema。
"""

import inspect
import json
import types
import typing
from typing import Any, Callable, Type, Union, Literal, get_args, get_origin

from pydantic import BaseModel
from pydantic.fields import FieldInfo

from app.agent.tools.core.base import BaseTool, ToolContext, ToolResult
from app.agent.tools.core.schema import tool_parameters_schema
from app.agent.utils.infra.log import log_tool_call_result

# Python 基础类型 → JSON Schema 类型
_SCALAR_JSON = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}

# 运行时注入、不进 schema 的参数名
_INJECTED_PARAM = "context"


def _unwrap_annotated(ann: Any) -> tuple[Any, str | None]:
    """剥离 Annotated[T, "说明"]，返回 (基础类型, 描述)。

    Args:
        ann (Any): 可能携带 Annotated 描述的类型注解。
    """
    if hasattr(ann, "__metadata__"):
        desc = next((m for m in ann.__metadata__ if isinstance(m, str)), None)
        return ann.__origin__, desc
    return ann, None


def _strip_titles(node: Any) -> None:
    """递归去掉 Pydantic 注入的 "title" 键（工具 schema 用不到）。

    Args:
        node (Any): 当前递归处理的 JSON Schema 节点。
    """
    if isinstance(node, dict):
        node.pop("title", None)
        for v in node.values():
            _strip_titles(v)
    elif isinstance(node, list):
        for v in node:
            _strip_titles(v)


def _inline_defs(node: Any, defs: dict) -> Any:
    """递归把 {"$ref": "#/$defs/X"} 替换为 defs[X]，使 schema 自包含（多网关更兼容）。

    Args:
        node (Any): 当前递归处理的 JSON Schema 节点。
        defs (dict): JSON Schema 中按名称保存的可复用定义。
    """
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            target = defs.get(ref.split("/")[-1], {})
            return _inline_defs(dict(target), defs)
        return {k: _inline_defs(v, defs) for k, v in node.items()}
    if isinstance(node, list):
        return [_inline_defs(v, defs) for v in node]
    return node


def _parameter_schema(parameter_type: Any) -> dict:
    """生成保留联合类型及 Field 约束的自包含参数 Schema，不发送类型文档。

    Args:
        parameter_type (Any): 参数模型类、联合类型，或通过 Annotated 声明 Field 约束的类型。

    Returns:
        dict: 保留显式字段说明与约束的自包含 Schema。
    """
    schema = tool_parameters_schema(parameter_type)
    defs = schema.pop("$defs", {})
    schema = _inline_defs(schema, defs)
    _strip_titles(schema)
    return schema


def _is_json_schema_default(value: Any) -> bool:
    """判断函数默认值是否可安全放入 JSON Schema 的 default 字段。

    Args:
        value (Any): 当前需要校验或转换的值。
    """
    try:
        json.dumps(value)
    except TypeError:
        return False
    return True


def _json_type(tp: Any) -> dict:
    """把一个 Python 类型映射成 JSON Schema 片段（{"type": ...} 等）。

    Args:
        tp (Any): 要转换为 JSON Schema 的 Python 类型。

    Returns:
        dict: 保留联合类型中 null 分支及其他允许类型的 Schema 片段。
    """
    origin = get_origin(tp)

    # 1. 联合类型必须保留全部分支，不能删掉 None，也不能将多类型联合缩成字符串。
    # 1.1. 使用共享参数生成器保留 null 分支；字段是否能省略仍由 _build_schema 的 required 决定。
    if origin is Union or origin is types.UnionType:
        return _parameter_schema(tp)

    # Literal 声明的可选值直接转换为模型参数的枚举限制。
    if origin is Literal:
        values = list(get_args(tp))
        scalar = _SCALAR_JSON.get(type(values[0]), "string") if values else "string"
        return {"type": scalar, "enum": values}

    # list[X] / tuple[X] → array，带上元素 schema
    if origin in (list, tuple):
        args = get_args(tp)
        if args:
            return {"type": "array", "items": _json_type(args[0])}
        return {"type": "array"}
    if origin is dict:
        return {"type": "object"}

    # Pydantic 模型 → 嵌套 object schema
    if isinstance(tp, type) and issubclass(tp, BaseModel):
        return _parameter_schema(tp)

    return {"type": _SCALAR_JSON.get(tp, "string")}


def _inspect_params(func: Callable) -> tuple[list[str], str | None]:
    """只读签名，返回 (送进 args 的参数名列表, 注入参数名 or None)，不合成 schema。

    供「显式传入 parameters_schema」的工具使用：跳过自动推导，但仍需知道哪些参数
    从 args 取、以及是否注入 context。

    Args:
        func (Callable): 需要包装或检查签名的函数。
    """
    names: list[str] = []
    injected: str | None = None
    for pname in inspect.signature(func).parameters:
        if pname == _INJECTED_PARAM:
            injected = pname
        else:
            names.append(pname)
    return names, injected


def _build_schema(func: Callable) -> tuple[dict, str | None]:
    """从函数签名合成 parameters_schema，返回 (schema, 注入参数名或 None)。

    Args:
        func (Callable): 需要包装或检查签名的函数。
    """
    sig = inspect.signature(func)
    hints = typing.get_type_hints(func, include_extras=True)

    properties: dict[str, dict] = {}
    required: list[str] = []
    injected: str | None = None

    for pname, param in sig.parameters.items():
        if pname == _INJECTED_PARAM:
            injected = pname
            continue
        ann = hints.get(pname, param.annotation)
        base, desc = _unwrap_annotated(ann)
        if not desc:
            raise ValueError(
                f"@tool 函数 '{func.__name__}' 的参数 '{pname}' 缺少 Annotated 描述："
                f"请写成 {pname}: Annotated[类型, \"说明\"]。"
            )
        # 1. 带 Field 的参数保留完整注解生成约束；其他参数沿用既有类型转换。
        # 1.1. 文字说明仍取自 Annotated 中的字符串，不从函数或参数模型文档读取。
        if any(isinstance(item, FieldInfo) for item in getattr(ann, "__metadata__", ())):
            prop = _parameter_schema(ann)
        else:
            prop = _json_type(base)
        prop["description"] = desc
        properties[pname] = prop
        if param.default is inspect.Parameter.empty:
            required.append(pname)
        elif _is_json_schema_default(param.default):
            prop["default"] = param.default

    schema: dict = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema, injected


def tool(
    func: Callable | None = None,
    *,
    description: str,
    name: str | None = None,
    is_resumable: bool = False,
    compressible: bool = False,
    persist_oversized_result: bool = True,
    authorization_timing: str = "before_execute",
    parameters_schema: dict | None = None,
) -> Any:
    """将显式模型说明与异步函数包装为 BaseTool 类，不读取函数的维护文档。

    用法：
        @tool(description="读取工作区文件。")
        async def read_file(path: Annotated[str, "文件路径"]) -> str:
            \"\"\"读取文件。\"\"\"
            ...

    Args:
        description (str): 必填的非空模型说明，描述工具用途、调用时机及操作约束。
        name (str | None): 覆盖工具名（默认取函数名）。
        is_resumable (bool): 标记为可恢复工具（execute 内靠 context.resume_data 分支）。
        compressible (bool): 标记旧工具结果允许从活跃模型上下文回收，不代表允许重放工具。
        persist_oversized_result (bool): 超过模型预览预算时是否允许保存完整结果。
        authorization_timing (str): 授权发生在执行前，或只读准备完成后。
        parameters_schema (dict | None): 逃生口——直接给定 JSON Schema，跳过自动推导。
            用于嵌套/复杂参数（如 update_plan 的 array-of-object），签名无法表达时。
        func (Callable | None): 需要包装或检查签名的函数。

    Returns:
        Any: 等待函数定义的装饰器，或包装已有函数后得到的 BaseTool 子类。

    Raises:
        ValueError: 模型说明不是字符串，或只包含空白字符。
    """
    # 1. 定义工具时立即检查模型说明，遗漏或无效时失败，不回退读取 docstring。
    if not isinstance(description, str) or not description.strip():
        raise ValueError("@tool 的 description 必须是非空字符串。")

    def wrap(fn: Callable) -> Type[BaseTool]:
        """装饰工具函数时从签名及说明创建 BaseTool 子类，此时不执行工具正文。

        Args:
            fn (Callable): 需要包装或执行的函数。

        Returns:
            Type[BaseTool]: 可供注册表与执行管道使用的工具类。
        """
        tool_name = name or fn.__name__
        # 1. 参数说明从 Annotated 或显式 Schema 读取，不包含运行时注入的 context。
        if parameters_schema is not None:
            schema = parameters_schema
            param_names, injected = _inspect_params(fn)
        else:
            schema, injected = _build_schema(fn)
            param_names = list(schema["properties"].keys())

        async def execute(self, args: dict, context: ToolContext) -> ToolResult:
            """执行管道调用动态工具时保留省略参数并注入上下文，完成后记录日志及包装结果。

            模型没有提供的参数不传给原函数，由 Python 应用函数默认值；
            显式提供的值（包括 None）原样转交。

            Args:
                args (dict): 模型实际提供的工具参数，不包含省略字段的占位值。
                context (ToolContext): 执行管道提供的工具运行上下文。

            Returns:
                ToolResult: 原 ToolResult 或由字符串包装的成功结果。
            """
            # 1. 只转交签名中声明且模型实际提供的参数，省略字段留给 Python 应用默认值。
            # 1.1. 根据字段是否存在筛选，不按值筛选，保留显式的零、False、空串和 None。
            kwargs: dict[str, Any] = {k: args[k] for k in param_names if k in args}
            # 1.2. 运行上下文始终由执行管道注入，不从模型参数读取。
            if injected:
                kwargs[injected] = context
            # 2. 原工具结束后记录输出，再统一为 ToolResult，已有中断和状态更新材料原样保留。
            result = await fn(**kwargs)
            log_payload = result if isinstance(result, str) else getattr(result, "content", "")
            await log_tool_call_result(tool_name, args, log_payload)
            if isinstance(result, ToolResult):
                return result
            return ToolResult.success(result)

        # 2. 动态类沿用普通 BaseTool 的注册、授权和执行接口，不在装饰阶段执行工具。
        cls_name = "".join(p.capitalize() for p in tool_name.split("_")) + "Tool"
        return type(cls_name, (BaseTool,), {
            "name": tool_name,
            "description": description,
            "parameters_schema": schema,
            "is_resumable": is_resumable,
            "compressible": compressible,
            "persist_oversized_result": persist_oversized_result,
            "authorization_timing": authorization_timing,
            "execute": execute,
            "_tool_func": staticmethod(fn),
        })

    return wrap(func) if func is not None else wrap
