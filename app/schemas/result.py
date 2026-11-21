from typing import Generic, TypeVar

from pydantic import BaseModel

T = TypeVar("T")


class Result(BaseModel, Generic[T]):
    """统一包装接口返回的数据、说明和业务状态码。

    Attributes:
        data (T): 当前接口声明的业务数据。
        msg (str): 本次操作的结果说明。
        code (int): 本次操作的业务状态码。
    """

    data: T
    msg: str
    code: int
