"""函数式工具装饰器 @tool。

把一个带类型注解 + docstring 的 async 函数，自动变成一个 BaseTool 子类：
- name        ← 函数名（或显式传入）
- description ← 函数 docstring
- parameters_schema ← 从签名 + 类型注解自动合成（结构）；每个参数的文字描述写在
                       Annotated[类型, "说明"] 里（必填，缺失则装饰时报错）
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

from app.agent.tools.core.base import BaseTool, ToolContext, ToolResult
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


def _model_schema(model: type[BaseModel]) -> dict:
    """从 Pydantic 模型生成自包含的 JSON Schema：内联 $defs + 去 title。

    Args:
        model (type[BaseModel]): 模型配置或模型类型。
    """
    schema = model.model_json_schema()
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
    """
    origin = get_origin(tp)

    # Optional[X] / X | None → 取非 None 的内层类型
    # 注意：typing.Optional[X] 的 origin 是 typing.Union；PEP604 的 X | None 是 types.UnionType
    if origin is Union or origin is types.UnionType:
        non_none = [a for a in get_args(tp) if a is not type(None)]
        if len(non_none) == 1:
            return _json_type(non_none[0])
        return {"type": "string"}  # 复杂联合，回退

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
        return _model_schema(tp)

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
    name: str | None = None,
    is_resumable: bool = False,
    compressible: bool = False,
    persist_oversized_result: bool = True,
    authorization_timing: str = "before_execute",
    parameters_schema: dict | None = None,
) -> Any:
    """将带文档和类型注解的异步函数变成 BaseTool 类，供注册、授权和执行管道统一使用。

    用法：
        @tool
        async def read_file(path: Annotated[str, "文件路径"]) -> str:
            \"\"\"读取文件。\"\"\"
            ...

    Args:
        name (str | None): 覆盖工具名（默认取函数名）。
        is_resumable (bool): 标记为可恢复工具（execute 内靠 context.resume_data 分支）。
        compressible (bool): 标记旧工具结果允许从活跃模型上下文回收，不代表允许重放工具。
        persist_oversized_result (bool): 超过模型预览预算时是否允许保存完整结果。
        authorization_timing (str): 授权发生在执行前，或只读准备完成后。
        parameters_schema (dict | None): 逃生口——直接给定 JSON Schema，跳过自动推导。
            用于嵌套/复杂参数（如 update_plan 的 array-of-object），签名无法表达时。
        func (Callable | None): 需要包装或检查签名的函数。
    """
    def wrap(fn: Callable) -> Type[BaseTool]:
        """装饰工具函数时从签名及说明创建 BaseTool 子类，此时不执行工具正文。

        Args:
            fn (Callable): 需要包装或执行的函数。

        Returns:
            Type[BaseTool]: 可供注册表与执行管道使用的工具类。
        """
        tool_name = name or fn.__name__
        # 1. 函数文档会成为模型可见工具说明；参数说明从 Annotated 读取，不包含注入的 context。
        description = inspect.getdoc(fn) or ""
        if parameters_schema is not None:
            schema = parameters_schema
            param_names, injected = _inspect_params(fn)
        else:
            schema, injected = _build_schema(fn)
            param_names = list(schema["properties"].keys())

        async def execute(self, args: dict, context: ToolContext) -> ToolResult:
            """执行管道调用动态工具时注入运行上下文，等待原函数结束后记录日志并统一包装结果。

            Args:
                args (dict): 当前工具调用或被包装函数的参数。
                context (ToolContext): 执行管道提供的工具运行上下文。

            Returns:
                ToolResult: 原 ToolResult 或由字符串包装的成功结果。
            """
            # 1. 只传签名中声明的工具参数，再注入运行上下文；不把整个状态对象交给模型。
            kwargs: dict[str, Any] = {k: args.get(k) for k in param_names}
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
