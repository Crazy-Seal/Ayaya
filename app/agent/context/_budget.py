"""主模型完整输入的 token 预算与真实基线校准。"""

from dataclasses import dataclass
from hashlib import sha256
from typing import Literal

from app.agent.context.config import ContextStrategyConfig
from app.agent.state.model import TokenBaseline
from app.agent.utils.domain.token_estimation import RoughTokenEstimator, stable_json


@dataclass(frozen=True)
class TokenAssessment:
    """一次待发送请求的 token 预算快照。"""

    rough_prompt_tokens: int
    estimated_prompt_tokens: int
    source: Literal["rough", "actual_delta"]
    accounting_key: str
    estimator_version: int
    static_fingerprint: str


def build_token_accounting_key(*, protocol: str, model: str, endpoint: str) -> str:
    """为不能共享真实 token 基线的模型调用配置生成稳定标识。"""
    value = stable_json({
        "protocol": protocol,
        "model": model,
        "endpoint": endpoint.rstrip("/"),
    })
    return sha256(value.encode("utf-8")).hexdigest()


class ContextBudgetService(RoughTokenEstimator):
    """以最近真实 prompt usage 为锚点估算当前完整主模型请求。"""

    ESTIMATOR_VERSION = 1

    def __init__(self, config: ContextStrategyConfig) -> None:
        self.config = config

    @staticmethod
    def _static_fingerprint(system_prompt: str, tools: list[dict] | None) -> str:
        value = stable_json({"system_prompt": system_prompt or "", "tools": tools or []})
        return sha256(value.encode("utf-8")).hexdigest()

    def assess(
        self,
        *,
        system_prompt: str,
        tools: list[dict] | None,
        messages: list[dict],
        baseline: TokenBaseline | None,
        accounting_key: str,
    ) -> TokenAssessment:
        """用真实基线加完整粗估净变化计算当前输入预算。"""
        rough = self.estimate_raw(
            system_prompt=system_prompt,
            tools=tools,
            messages=messages,
        )
        fingerprint = self._static_fingerprint(system_prompt, tools)
        compatible = (
            baseline is not None
            and baseline.accounting_key == accounting_key
            and baseline.estimator_version == self.ESTIMATOR_VERSION
            and baseline.static_fingerprint == fingerprint
        )
        if compatible:
            estimated = max(
                1,
                baseline.actual_prompt_tokens
                + rough
                - baseline.rough_prompt_tokens,
            )
            source: Literal["rough", "actual_delta"] = "actual_delta"
        else:
            estimated = rough
            source = "rough"
        return TokenAssessment(
            rough_prompt_tokens=rough,
            estimated_prompt_tokens=estimated,
            source=source,
            accounting_key=accounting_key,
            estimator_version=self.ESTIMATOR_VERSION,
            static_fingerprint=fingerprint,
        )

    @staticmethod
    def observe(
        assessment: TokenAssessment,
        *,
        actual_prompt_tokens: int,
    ) -> TokenBaseline | None:
        """把成功请求的真实 prompt usage 固化为下一次预算锚点。"""
        if actual_prompt_tokens <= 0:
            return None
        return TokenBaseline(
            accounting_key=assessment.accounting_key,
            estimator_version=assessment.estimator_version,
            static_fingerprint=assessment.static_fingerprint,
            rough_prompt_tokens=assessment.rough_prompt_tokens,
            actual_prompt_tokens=actual_prompt_tokens,
        )

    def should_compact(self, estimated_tokens: int) -> bool:
        return estimated_tokens >= self.config.compression_trigger_tokens


__all__ = [
    "ContextBudgetService",
    "RoughTokenEstimator",
    "TokenAssessment",
    "build_token_accounting_key",
]
