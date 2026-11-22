"""本机后端健康检查与进程控制的 HTTP 响应。"""

from pydantic import BaseModel


class RootResponse(BaseModel):
    """确认本机后端 HTTP 服务可访问。

    Attributes:
        message (str): 服务启动提示。
    """

    message: str


class ReadyResponse(BaseModel):
    """向 Electron 交付后端就绪状态和进程身份。

    Attributes:
        ready (bool): 后端已准备好处理请求。
        pid (int): 当前后端进程编号。
    """

    ready: bool
    pid: int


class ActiveTaskSummary(BaseModel):
    """为退出确认交付仍在执行或保存的任务数量。

    Attributes:
        active_count (int): 尚未结束的任务数量。
        application_boot_active_count (int): 活动任务中由插件启动声明挂载的数量。
    """

    active_count: int
    application_boot_active_count: int


class ShutdownResponse(BaseModel):
    """确认后端已开始执行优雅关闭。

    Attributes:
        shutting_down (bool): 关闭请求已经受理。
    """

    shutting_down: bool


class HealthResponse(BaseModel):
    """交付会话使用的模型和后端健康状态。

    Attributes:
        status (str): 后端健康状态。
        model (str): 当前会话的模型名称。
    """

    status: str
    model: str
