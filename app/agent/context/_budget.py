"""主模型完整输入的 token 预算与真实基线校准。"""

from dataclasses import dataclass
from hashlib import sha256
from typing import Literal

from app.agent.context.config import ContextStrategyConfig
from app.agent.state.model import TokenBaseline
from app.agent.utils.domain.token_estimation import RoughTokenEstimator, stable_json


@dataclass(frozen=True)
class TokenAssessment:
    """记录一次完整模型请求的估算结果，供压缩判断和成功后的用量校准使用。

    Attributes:
        rough_prompt_tokens (int): 按本地规则粗估的输入 token 数。
        estimated_prompt_tokens (int): 使用兼容的真实用量基线校准后的输入 token 数。
        source (Literal["rough", "actual_delta"]): 本次使用粗估，还是在真实用量上叠加粗估差值。
        accounting_key (str): 模型、协议和地址组成的计量配置标识。
        estimator_version (int): 粗估规则版本，版本不同则不能复用基线。
        static_fingerprint (str): 系统提示词和工具定义的摘要，用于判断基线是否兼容。
    """

    rough_prompt_tokens: int
    estimated_prompt_tokens: int
    source: Literal["rough", "actual_delta"]
    accounting_key: str
    estimator_version: int
    static_fingerprint: str


def build_token_accounting_key(*, protocol: str, model: str, endpoint: str) -> str:
    """区分模型、调用协议与服务地址，避免把另一种配置的真实用量用于当前请求。

    Args:
        protocol (str): 当前模型调用协议名称。
        model (str): 模型名称。
        endpoint (str): 模型服务地址，末尾斜杠不影响标识。

    Returns:
        str: 计量配置的 SHA-256 摘要。
    """
    value = stable_json({
        "protocol": protocol,
        "model": model,
        "endpoint": endpoint.rstrip("/"),
    })
    return sha256(value.encode("utf-8")).hexdigest()


class ContextBudgetService(RoughTokenEstimator):
    """在模型调用前估算输入大小，决定是否需要压缩会话历史。

    最近一次成功请求的真实用量只用于校准；没有兼容基线时使用本地粗估。

    Attributes:
        config (ContextStrategyConfig): 会话配置的压缩阈值和截图保留策略。
    """

    ESTIMATOR_VERSION = 1

    def __init__(self, config: ContextStrategyConfig) -> None:
        """保存会话的上下文策略，供每次模型调用前检查输入大小。

        Args:
            config (ContextStrategyConfig): 压缩阈值及截图保留配置。
        """
        self.config = config

    @staticmethod
    def _static_fingerprint(system_prompt: str, tools: list[dict] | None) -> str:
        """计算系统提示词和工具定义的摘要，用于拒绝已经失效的用量基线。

        Args:
            system_prompt (str): 当前请求使用的系统提示词。
            tools (list[dict] | None): 当前向模型公开的工具定义。

        Returns:
            str: 静态请求内容的 SHA-256 摘要。
        """
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
        """估算完整模型输入；配置兼容时，在真实用量上叠加消息增删产生的粗估差值。

        Args:
            system_prompt (str): 当前模型系统提示词。
            tools (list[dict] | None): 本次公开的工具定义。
            messages (list[dict]): 已转换为模型格式的历史消息。
            baseline (TokenBaseline | None): 上次成功调用保存的真实用量及对应粗估值。
            accounting_key (str): 当前模型、协议和地址的计量配置标识。

        Returns:
            TokenAssessment: 当前输入的粗估值、校准值及基线兼容信息。
        """
        rough = self.estimate_raw(
            system_prompt=system_prompt,
            tools=tools,
            messages=messages,
        )
        fingerprint = self._static_fingerprint(system_prompt, tools)
        # 1. 模型配置、粗估版本和系统提示词/工具定义都一致，才复用上次真实用量。
        compatible = (
            baseline is not None
            and baseline.accounting_key == accounting_key
            and baseline.estimator_version == self.ESTIMATOR_VERSION
            and baseline.static_fingerprint == fingerprint
        )
        if compatible:
            # 2. 用当前与上次粗估的净差值校准，消息被替换或删除时也能反映输入变小。
            estimated = max(
                1,
                baseline.actual_prompt_tokens
                + rough
                - baseline.rough_prompt_tokens,
            )
            source: Literal["rough", "actual_delta"] = "actual_delta"
        else:
            # 2. 没有可复用基线时退回本地粗估，避免引用另一种模型请求的用量。
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
        """把成功请求的真实输入用量与调用前的估算结果配对，供下一次估算使用。

        Args:
            assessment (TokenAssessment): 发起该请求前保存的完整输入估算结果。
            actual_prompt_tokens (int): 模型服务返回的真实输入 token 数。

        Returns:
            TokenBaseline | None: 可用于后续校准的基线；用量为零或负数时不创建基线。
        """
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
        """判断当前输入是否已达到会话配置的压缩阈值。

        Args:
            estimated_tokens (int): 完整模型输入的估算 token 数。

        Returns:
            bool: 达到或超过阈值时为 True。
        """
        return estimated_tokens >= self.config.compression_trigger_tokens


__all__ = [
    "ContextBudgetService",
    "RoughTokenEstimator",
    "TokenAssessment",
    "build_token_accounting_key",
]
