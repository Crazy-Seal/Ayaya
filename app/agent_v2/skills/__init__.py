"""Skill（能力包）系统。

一个 Skill = 系统提示词片段 + 一组工具 + 可选插件，按会话开关。
"""

from app.agent_v2.skills.base import BaseSkill
from app.agent_v2.skills.registry import SkillRegistry

__all__ = ["BaseSkill", "SkillRegistry"]
