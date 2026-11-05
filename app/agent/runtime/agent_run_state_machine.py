"""集中校验同一上下文各轮执行的阶段、轮次及等待编号转换。"""

from app.schemas.runtime_types import InterruptKind, TurnPhase


class InvalidStateTransitionError(RuntimeError):
    """调用方以过期轮次或错误阶段推进执行时报告程序错误。"""

    def __init__(self, run_id: str, source: TurnPhase, target: TurnPhase) -> None:
        """保留轮次及转换两端，便于定位错误调用。

        Args:
            run_id (str): 本次动作要求操作的轮次编号。
            source (TurnPhase): 发起转换时的运行阶段。
            target (TurnPhase): 本次动作要求进入的运行阶段。
        """
        self.run_id, self.source, self.target = run_id, source, target
        super().__init__(f"运行阶段转换无效：{run_id}，{source.value} -> {target.value}")


class AgentRunStateMachine:
    """由会话持有并借给各轮控制对象的唯一运行阶段。

    Attributes:
        phase (TurnPhase): 只读的执行阶段，持久化成功后才确认暂停或结束。
        run_id (str | None): 当前轮次身份。
        interrupt_kind (InterruptKind | None): 当前已保存等待的类别。
        request_id (str | None): 当前已保存等待的请求身份。
    """

    def __init__(self) -> None:
        """建立尚未开始工作的上下文。"""
        self._phase = TurnPhase.IDLE
        self._run_id: str | None = None
        self._interrupt_kind: InterruptKind | None = None
        self._request_id: str | None = None

    @property
    def phase(self) -> TurnPhase:
        """返回当前阶段。"""
        return self._phase

    @property
    def run_id(self) -> str | None:
        """返回当前轮次身份。"""
        return self._run_id

    @property
    def interrupt_kind(self) -> InterruptKind | None:
        """返回已保存等待的类别。"""
        return self._interrupt_kind

    @property
    def request_id(self) -> str | None:
        """返回已保存等待的请求身份。"""
        return self._request_id

    def _transition(self, run_id: str, target: TurnPhase, allowed: set[TurnPhase]) -> None:
        """校验轮次和来源阶段，失败时保留原状态。

        Args:
            run_id (str): 发起转换的轮次。
            target (TurnPhase): 目标阶段。
            allowed (set[TurnPhase]): 此动作允许的来源阶段。

        Raises:
            InvalidStateTransitionError: 轮次已过期或来源阶段不合法。
        """
        if run_id != self._run_id or self._phase not in allowed:
            raise InvalidStateTransitionError(run_id, self._phase, target)
        self._phase = target

    def begin_run(self, run_id: str) -> None:
        """旧轮已收尾或上下文空闲时开始新轮次。

        Args:
            run_id (str): 本次需要开始的轮次编号。
        """
        if self._phase not in {TurnPhase.IDLE, TurnPhase.FINISHING}:
            raise InvalidStateTransitionError(run_id, self._phase, TurnPhase.PREPARING)
        self._run_id = run_id
        self._phase = TurnPhase.PREPARING
        self._interrupt_kind = None
        self._request_id = None

    def accepts_interrupt(self, run_id: str) -> bool:
        """判断停止或引导是否仍可作用于该轮次。

        Args:
            run_id (str): 本次请求停止或引导的轮次编号。
        """
        return self._run_id == run_id and self._phase not in {
            TurnPhase.IDLE, TurnPhase.COMMITTING, TurnPhase.FINISHING,
        }

    def start_llm(self, run_id: str) -> None:
        """在准备、工具完成或模型重试后开始模型请求。

        Args:
            run_id (str): 本次需要进入模型阶段的轮次编号。
        """
        self._transition(run_id, TurnPhase.LLM, {TurnPhase.PREPARING, TurnPhase.LLM, TurnPhase.TOOL})

    def start_tool(self, run_id: str) -> None:
        """模型给出调用或审批恢复后开始工具执行。

        Args:
            run_id (str): 本次需要进入工具阶段的轮次编号。
        """
        self._transition(run_id, TurnPhase.TOOL, {TurnPhase.PREPARING, TurnPhase.LLM, TurnPhase.TOOL})

    def interrupt_saved(self, run_id: str, kind: InterruptKind, request_id: str) -> None:
        """完整中断提交成功后确认等待身份。

        Args:
            run_id (str): 已经完成中断保存的轮次编号。
            kind (InterruptKind): 本次已经确定的业务类别。
            request_id (str): 需要匹配的持久等待请求编号。
        """
        kind = InterruptKind(kind)
        self._transition(run_id, TurnPhase.INTERRUPTED, {TurnPhase.TOOL})
        self._interrupt_kind, self._request_id = kind, request_id

    def prepare_resume(self, run_id: str, request_id: str) -> bool:
        """匹配当前审批后恢复原轮，过期请求返回未受理。

        Args:
            run_id (str): 本次等待恢复的原轮次编号。
            request_id (str): 需要匹配的持久等待请求编号。
        """
        if (run_id != self._run_id or request_id != self.request_id
                or self._phase != TurnPhase.INTERRUPTED
                or self.interrupt_kind != InterruptKind.AUTHORIZATION_REQUEST):
            return False
        self._transition(run_id, TurnPhase.PREPARING, {TurnPhase.INTERRUPTED})
        self._interrupt_kind = None
        self._request_id = None
        return True

    def begin_commit(self, run_id: str) -> None:
        """通过最后一个安全点后保护最终回答提交。

        Args:
            run_id (str): 本次开始提交最终回答的轮次编号。
        """
        self._transition(run_id, TurnPhase.COMMITTING, {TurnPhase.PREPARING, TurnPhase.LLM, TurnPhase.TOOL})

    def begin_finishing(self, run_id: str) -> None:
        """回答已保存或执行失败交还后进入资源和报告收尾。

        Args:
            run_id (str): 本次开始资源与报告收尾的轮次编号。
        """
        self._transition(run_id, TurnPhase.FINISHING, {
            TurnPhase.PREPARING, TurnPhase.LLM, TurnPhase.TOOL, TurnPhase.INTERRUPTED,
            TurnPhase.COMMITTING, TurnPhase.FINISHING,
        })
        self._interrupt_kind = None
        self._request_id = None

    def complete(self, run_id: str) -> None:
        """报告及本次资源交还完成后恢复空闲。

        Args:
            run_id (str): 已完成报告和资源交还的轮次编号。
        """
        self._transition(run_id, TurnPhase.IDLE, {TurnPhase.FINISHING})
        self._run_id = None
