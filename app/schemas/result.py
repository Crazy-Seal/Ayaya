from pydantic import BaseModel

class Result(BaseModel):
    # 结果包装类
    """统一包装接口返回的数据、说明和业务状态码。"""
    data: object
    msg: str
    code: int