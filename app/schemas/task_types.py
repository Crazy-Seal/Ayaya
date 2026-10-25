"""后台任务记录与插件请求的数据定义，不创建执行对象或队列。"""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from app.schemas.runtime_types import TaskLifetime, TaskStatus

TERMINAL_TASK_NAME = "terminal"


@dataclass(frozen=True)
class TaskRecord:
    """保存一次任务的状态、结果、来源以及实际存在的终端信息。

    Attributes:
        task_id (str): 区分本次执行的稳定编号，查询和停止时使用。
        session_id (str): 创建任务的会话编号。
        command (str | None): 终端实际命令，通用任务为空。
        status (TaskStatus): 已保存的运行或终态。
        pid (int | None): 终端系统进程编号，通用任务为空。
        exit_code (int | None): 实际终端退出码，通用任务或进程未结束时为空。
        output_path (str | None): 完整终端输出文件路径，通用任务为空。
        created_at (str): 登记任务的 UTC 时间。
        ended_at (str | None): 保存终态的 UTC 时间，运行中为空。
        origin_run_id (str | None): 创建任务的固定来源轮次，跨轮运行不更新。
        origin_tool_call_id (str | None): 创建终端的工具调用编号，插件提交时为空。
        output_ref (str | None): 终端输出引用使用的任务编号，通用任务为空。
        background (bool): 是否已移交后台并纳入任务列表。
        tool_name (str | None): 创建终端的工具名称，插件任务为空。
        name (str): 插件定义的稳定任务名称，可用于多次执行。
        title (str): 本次任务给用户阅读的说明。
        result (str | None): run 返回的结果正文，清理失败也保留已取得结果。
        error (str | None): 执行或清理失败的错误摘要。
        plugin_name (str | None): 提交任务的插件名称，内置终端为空。
        lifetime (TaskLifetime): 随来源轮次结束或独立运行。
        notify_agent (bool): 终态保存时是否原子写入唯一结果通知。
    """

    task_id: str
    session_id: str
    command: str | None
    status: TaskStatus
    pid: int | None
    exit_code: int | None
    output_path: str | None
    created_at: str
    ended_at: str | None
    origin_run_id: str | None = None
    origin_tool_call_id: str | None = None
    output_ref: str | None = None
    background: bool = True
    tool_name: str | None = None
    name: str = TERMINAL_TASK_NAME
    title: str = ""
    result: str | None = None
    error: str | None = None
    plugin_name: str | None = None
    lifetime: TaskLifetime = TaskLifetime.INDEPENDENT
    notify_agent: bool = True


class PluginRequestKind(StrEnum):
    """区分需要 Scheduler 受理的插件动作。"""

    NOTIFY = "notify"
    STOP_TASK = "stop_task"
    STOP_TURN = "stop_turn"
    STEER_TURN = "steer_turn"
    PUBLISH_EVENT = "publish_event"


@dataclass(frozen=True)
class PluginRequest:
    """携带固定来源的一次单向请求，不包含受理回执或等待对象。

    Attributes:
        kind (PluginRequestKind): 通知、任务停止、轮次控制或前端发布。
        plugin_name (str): 发出请求的插件名称。
        session_id (str): 请求所属会话。
        run_id (str): 固定来源轮次，控制请求只能作用于此编号。
        source_task_id (str | None): 发出请求的后台任务编号，钩子中为空。
        task_id (str | None): 停止任务请求的目标编号，区别于请求来源。
        name (str): 通知稳定名称或框架前端事件名称。
        text (str): 持久通知或引导正文。
        payload (dict[str, Any]): 发布前端事件时已复制的结构化内容。
    """

    kind: PluginRequestKind
    plugin_name: str
    session_id: str
    run_id: str
    source_task_id: str | None = None
    task_id: str | None = None
    name: str = ""
    text: str = ""
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TaskStopRequest:
    """工具发出的单向停止请求，来源固定为发出请求的调用。

    Attributes:
        session_id (str): 来源会话，限制目标任务归属。
        run_id (str): 发出请求的轮次。
        tool_name (str): 发出请求的工具。
        tool_call_id (str): 发出请求的模型调用编号。
        task_id (str): 目标任务编号。
    """

    session_id: str
    run_id: str
    tool_name: str
    tool_call_id: str
    task_id: str


RuntimeRequest = TaskRecord | PluginRequest | TaskStopRequest | None
