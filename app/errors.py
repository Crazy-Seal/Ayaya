"""表达资源查询、保存冲突及业务操作被拒绝的原因。"""


class ResourceNotFoundError(LookupError):
    """指定资源不存在，或不属于当前查询范围。"""


class ResourceConflictError(ValueError):
    """资源编号重复，或修改违反已有资源的标识约束。"""


class InvalidOperationError(ValueError):
    """请求内容可以解析，但当前业务不允许执行该操作。"""
