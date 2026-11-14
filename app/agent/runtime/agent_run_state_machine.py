"""集中管理一个实际上下文的执行阶段、等待和控制请求。"""


from app.schemas.runtime_types import ApprovalWait, CommandReceipt, CommandStatus, ExecutionPhase, ExecutionSnapshot, ExecutionWait, TurnInterruptReason


class InvalidStateTransitionError(RuntimeError):
    """内部代码以错误阶段或轮次推进时，报告错误并保留原状态。"""


# 每张表只校验本层阶段；请求身份和对应材料由业务方法同步维护。
EXECUTION_EDGES: dict[ExecutionPhase, frozenset[ExecutionPhase]] = {
    ExecutionPhase.IDLE: frozenset({ExecutionPhase.RUNNING}),
    ExecutionPhase.RUNNING: frozenset({ExecutionPhase.WAITING, ExecutionPhase.STOPPING,
                                     ExecutionPhase.COMMITTING, ExecutionPhase.FAULTED}),
    ExecutionPhase.WAITING: frozenset({ExecutionPhase.RUNNING, ExecutionPhase.STOPPING, ExecutionPhase.FAULTED}),
    ExecutionPhase.STOPPING: frozenset({ExecutionPhase.COMMITTING, ExecutionPhase.FAULTED}),
    ExecutionPhase.COMMITTING: frozenset({ExecutionPhase.IDLE, ExecutionPhase.RUNNING, ExecutionPhase.FAULTED}),
    ExecutionPhase.FAULTED: frozenset({ExecutionPhase.IDLE}),
}
class AgentRunStateMachine:
    """集中验证执行操作，所有耗时业务由调用者顺序执行。

    Attributes:
        _context_id (str): 固定实际上下文编号。
        _phase (ExecutionPhase): 唯一执行阶段。
        _run_id (str | None): 当前轮次编号。
        _wait (ExecutionWait | None): WAITING 对应的等待材料。
        _reason (TurnInterruptReason | None): STOPPING 及其协议保存对应的控制原因。
        _input_id (str | None): 引导对应的已保存输入编号。
        _error (str | None): FAULTED 对应的保存故障。
    """

    def __init__(self, context_id: str) -> None:
        """建立空闲的上下文执行对象。

        Args:
            context_id (str): 实际父或子上下文编号。
        """
        self._context_id = context_id
        self._phase = ExecutionPhase.IDLE
        self._run_id: str | None = None
        self._wait: ExecutionWait | None = None
        self._reason: TurnInterruptReason | None = None
        self._input_id: str | None = None
        self._error: str | None = None

    def _change(self, target: ExecutionPhase) -> None:
        """校验本层转换后修改唯一阶段。

        Args:
            target (ExecutionPhase): 当前操作要求的阶段。
        Raises:
            InvalidStateTransitionError: 来源阶段不允许该操作。
        """
        if target not in EXECUTION_EDGES[self._phase]:
            raise InvalidStateTransitionError(f"执行阶段错误：{self._phase} -> {target}")
        self._phase = target

    def _require_run(self, run_id: str) -> None:
        """校验异步完成操作归属的轮次。

        Args:
            run_id (str): 原业务调用持有的轮次编号。
        Raises:
            InvalidStateTransitionError: 旧调用试图推进其他轮次。
        """
        if run_id != self._run_id:
            raise InvalidStateTransitionError(f"执行轮次已过期：{run_id}")

    def _receipt(self, status: CommandStatus) -> CommandReceipt:
        """返回当前控制操作的关联信息。

        Args:
            status (CommandStatus): 控制请求的处理结果。
        Returns:
            CommandReceipt: 当前上下文和轮次对应的回执。
        """
        return CommandReceipt(status, self._context_id, self._run_id)

    def begin_run(self, run_id: str) -> None:
        """空闲时开始已经受理的下一批输入。

        Args:
            run_id (str): 新轮次编号。
        Raises:
            InvalidStateTransitionError: 当前仍有执行或保存故障。
        """
        if self._phase != ExecutionPhase.IDLE:
            raise InvalidStateTransitionError("开始新轮需要空闲执行")
        self._change(ExecutionPhase.RUNNING)
        self._run_id = run_id

    def request_stop(self, expected_run_id: str | None = None) -> CommandReceipt:
        """请求停止当前执行，最终保存和旧请求分别明确拒绝。

        Args:
            expected_run_id (str | None): 显式目标轮次，省略时作用于当前执行。
        Returns:
            CommandReceipt: accepted、stale 或 too_late。
        """
        return self._request_interrupt(TurnInterruptReason.STOP, None, expected_run_id)

    def request_steer(self, input_id: str, expected_run_id: str | None = None) -> CommandReceipt:
        """用已保存的明确输入引导当前执行。

        Args:
            input_id (str): 已保存的输入编号。
            expected_run_id (str | None): 显式引导指定轮次时提供。
        Returns:
            CommandReceipt: 引导控制的结果；消息保存回执由 Session 单独提供。
        """
        return self._request_interrupt(TurnInterruptReason.STEER, input_id, expected_run_id)

    def _request_interrupt(self, reason: TurnInterruptReason, input_id: str | None,
                           expected_run_id: str | None) -> CommandReceipt:
        """受理本轮停止或引导，用户停止优先于尚未结束的引导。

        Args:
            reason (TurnInterruptReason): 停止或引导。
            input_id (str | None): 引导输入身份。
            expected_run_id (str | None): 可选目标轮次。
        Returns:
            CommandReceipt: 本地控制受理结果。
        """
        if expected_run_id is not None and expected_run_id != self._run_id:
            return self._receipt(CommandStatus.STALE)
        if self._phase == ExecutionPhase.STOPPING:
            # 1. 停止接管仍在等待工具边界的引导，消息正文继续由 Inbox 保存。
            if reason == TurnInterruptReason.STOP and self._reason == TurnInterruptReason.STEER:
                self._reason, self._input_id = TurnInterruptReason.STOP, None
                return self._receipt(CommandStatus.ACCEPTED)
            status = (CommandStatus.ACCEPTED if self._reason == reason and self._input_id == input_id
                      else CommandStatus.TOO_LATE)
            return self._receipt(status)
        if self._phase == ExecutionPhase.IDLE:
            return self._receipt(CommandStatus.STALE)
        if self._phase in {ExecutionPhase.COMMITTING, ExecutionPhase.FAULTED}:
            return self._receipt(CommandStatus.TOO_LATE)
        self._change(ExecutionPhase.STOPPING)
        self._reason, self._input_id = reason, input_id
        self._wait = None
        return self._receipt(CommandStatus.ACCEPTED)

    def wait_saved(self, run_id: str, wait: ExecutionWait) -> CommandReceipt:
        """等待记录保存后确认请求；保存期间的停止保持有效。

        Args:
            run_id (str): 保存等待的原轮次。
            wait (ExecutionWait): 已保存的审批或父消息等待。
        Returns:
            CommandReceipt: 过期、已经停止或正常进入等待的结果。
        """
        if run_id != self._run_id:
            return self._receipt(CommandStatus.STALE)
        if self._phase == ExecutionPhase.STOPPING:
            return self._receipt(CommandStatus.TOO_LATE)
        self._change(ExecutionPhase.WAITING)
        self._wait = wait
        return self._receipt(CommandStatus.ACCEPTED)

    def resume_approval(self, run_id: str, request_id: str) -> CommandReceipt:
        """匹配已保存审批后恢复原轮。

        Args:
            run_id (str): 审批原轮次编号。
            request_id (str): 已确认决定的审批编号。
        Returns:
            CommandReceipt: 匹配时受理，重复或过期时 stale。
        """
        if (run_id != self._run_id or self._phase != ExecutionPhase.WAITING
                or not isinstance(self._wait, ApprovalWait) or self._wait.request_id != request_id):
            return self._receipt(CommandStatus.STALE)
        self._change(ExecutionPhase.RUNNING)
        self._wait = None
        return self._receipt(CommandStatus.ACCEPTED)

    def begin_commit(self, run_id: str) -> None:
        """模型工具已经结束、材料准备完成后保护最终保存。

        Args:
            run_id (str): 开始保存的轮次。
        """
        self._require_run(run_id)
        self._change(ExecutionPhase.COMMITTING)

    def commit_succeeded(self, run_id: str, next_run_id: str | None = None) -> None:
        """保存完成后回空闲，或开始引导的新轮次。

        Args:
            run_id (str): 完成保存的旧轮次。
            next_run_id (str | None): 引导的新轮次，普通结束时为空。
        Raises:
            InvalidStateTransitionError: 旧保存回调或非法引导切换。
        """
        self._require_run(run_id)
        if self._phase != ExecutionPhase.COMMITTING:
            raise InvalidStateTransitionError("保存成功需要处于最终保存阶段")
        if next_run_id is not None and (self._reason != TurnInterruptReason.STEER or next_run_id == run_id):
            raise InvalidStateTransitionError("引导需要已受理的输入及新的轮次编号")
        self._change(ExecutionPhase.RUNNING if next_run_id is not None else ExecutionPhase.IDLE)
        self._run_id = next_run_id
        self._reason, self._input_id = None, None

    def commit_failed(self, run_id: str, error: str) -> None:
        """必需保存耗尽后终止本轮，等待读取最后已保存上下文。

        Args:
            run_id (str): 保存失败的轮次。
            error (str): 保存故障说明。
        """
        self._require_run(run_id)
        self._change(ExecutionPhase.FAULTED)
        self._wait = None
        self._reason, self._input_id = None, None
        self._error = error

    def restore_succeeded(self, run_id: str) -> None:
        """最后已保存上下文读取成功后回到空闲。

        Args:
            run_id (str): 本次恢复对应的故障轮次。
        """
        self._require_run(run_id)
        if self._phase != ExecutionPhase.FAULTED:
            raise InvalidStateTransitionError("恢复成功需要对应保存故障")
        self._change(ExecutionPhase.IDLE)
        self._run_id, self._error = None, None

    def snapshot(self) -> ExecutionSnapshot:
        """复制当前执行数据供只读查询。

        Returns:
            ExecutionSnapshot: 只包含当前阶段匹配的数据。
        """
        return ExecutionSnapshot(self._context_id, self._run_id, self._phase, self._wait, self._error)


    @property
    def phase(self) -> ExecutionPhase:
        """读取本上下文的唯一执行阶段。"""
        return self._phase

    @property
    def run_id(self) -> str | None:
        """读取当前执行编号，空闲时为空。"""
        return self._run_id

    @property
    def reason(self) -> TurnInterruptReason | None:
        """读取当前停止或引导所保留的首个原因。"""
        return self._reason

    @property
    def wait(self) -> ExecutionWait | None:
        """读取与 WAITING 一起维护的已保存请求。"""
        return self._wait

    def check_running(self, run_id: str) -> None:
        """在模型和工具启动前校验当前执行身份及运行阶段。

        Args:
            run_id (str): 即将执行模型或工具的轮次编号。
        """
        self._require_run(run_id)
        if self._phase != ExecutionPhase.RUNNING:
            raise InvalidStateTransitionError(f"当前阶段不能启动模型或工具：{self._phase}")
