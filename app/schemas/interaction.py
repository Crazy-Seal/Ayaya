"""交互命令、渠道订阅和桌面执行器的 HTTP 请求模型。"""

from pydantic import BaseModel, Field


class ChannelCommandRequest(BaseModel):
    """定义统一 HTTP 命令请求，具体 kind 的业务约束由 Scheduler 验证。

    Attributes:
        session_id (str): 共享 Ayaya 会话标识。
        origin_channel (str): 输入或本轮回复所属的渠道。
        kind (str): 此契约对应的命令、事件或输入类别。
        target_run_id (str | None): 停止或引导针对的活动 run，省略时按命令规则处理。
        request_id (str | None): checkpoint 中的持久中断标识。
        task_id (str | None): 受管进程任务标识。
        payload (dict): 该类别携带的专用载荷。
    """
    session_id: str = Field(min_length=1)
    origin_channel: str = Field(min_length=1)
    # 未知命令仍由 Scheduler 业务拒绝，不因枚举整理变成 HTTP 422。
    kind: str
    target_run_id: str | None = None
    request_id: str | None = None
    task_id: str | None = None
    payload: dict = Field(default_factory=dict)


class ChannelSubscriptionRequest(BaseModel):
    """定义可选渠道的会话订阅启用设置。

    Attributes:
        enabled (bool): 渠道是否参与该会话的双向互通。
    """
    enabled: bool
