"""交互命令与渠道订阅的 HTTP 请求和响应模型。"""

from pydantic import BaseModel, Field

from app.schemas.runtime_types import CommandStatus


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
    subagent_id: str | None = None
    payload: dict = Field(default_factory=dict)


class ChannelSubscriptionRequest(BaseModel):
    """定义可选渠道的会话订阅启用设置。

    Attributes:
        enabled (bool): 渠道是否参与该会话的双向互通。
    """
    enabled: bool


class CommandReceiptResponse(BaseModel):
    """向命令调用方交付受理结果及实际目标。

    Attributes:
        status (CommandStatus): 命令的业务处理结果。
        accepted (bool): 规定的受理操作是否完成。
        context_id (str | None): 实际目标上下文编号。
        run_id (str | None): 本次操作关联的轮次编号。
        detail (str | None): 拒绝或失败的说明。
    """

    status: CommandStatus
    accepted: bool
    context_id: str | None
    run_id: str | None
    detail: str | None


class ChannelSubscriptionResponse(BaseModel):
    """向界面交付一个渠道的订阅与可用状态。

    Attributes:
        channel (str): 已注册渠道的标识。
        display_name (str): 界面显示的渠道名称。
        enabled (bool): 当前会话是否启用该渠道。
        ready (bool): 渠道是否已经准备好交互。
    """

    channel: str
    display_name: str
    enabled: bool
    ready: bool


class ChannelListResponse(BaseModel):
    """交付当前会话的全部渠道配置。

    Attributes:
        channels (list[ChannelSubscriptionResponse]): 渠道订阅列表。
    """

    channels: list[ChannelSubscriptionResponse]
