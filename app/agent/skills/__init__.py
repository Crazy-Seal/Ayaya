"""Skill（能力包）系统。

一个 Skill = 系统提示词片段 + 一组工具 + 可选插件，按会话开关。
"""

from app.agent.skills.base import BaseSkill
from app.agent.skills.registry import SkillRegistry

__all__ = ["BaseSkill", "SkillRegistry"]
