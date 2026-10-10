"""Skill 注册表 - 支持延迟加载（仿 ToolRegistry）。"""

import logging
from importlib import import_module
from typing import Callable, Type

from app.agent.skills.base import BaseSkill

logger = logging.getLogger(__name__)


class SkillRegistry:
    """能力包注册表。"""

    _skills: dict[str, Type[BaseSkill]] = {}
    _lazy_skills: dict[str, str] = {}

    @classmethod
    def register(cls, name: str | None = None) -> Callable[[Type[BaseSkill]], Type[BaseSkill]]:
        """生成技能类注册装饰器，显式名称优先于技能类声明的名称。

        Args:
            name (str | None): 当前对象的名称。

        Returns:
            Callable[[Type[BaseSkill]], Type[BaseSkill]]: 登记技能类并原样返回该类的装饰器。
        """
        def decorator(skill_class: Type[BaseSkill]) -> Type[BaseSkill]:
            """定义技能类时把它登记到进程内技能注册表，供 Agent 后续装配。

            Args:
                skill_class (Type[BaseSkill]): 需要注册的技能类。

            Returns:
                Type[BaseSkill]: 传入的原技能类。
            """
            skill_name = name or skill_class.name
            cls._skills[skill_name] = skill_class
            logger.debug("注册 Skill: %s", skill_name)
            return skill_class
        return decorator

    @classmethod
    def register_lazy(cls, name: str, spec: str) -> None:
        """登记技能的模块和类名，实际请求该技能时才导入。

        Args:
            name (str): 当前对象的名称。
            spec (str): 延迟加载目标，采用模块路径或 模块路径:类名 格式。
        """
        cls._lazy_skills[name] = spec

    @classmethod
    def get(cls, name: str) -> Type[BaseSkill] | None:
        """按名称取得技能类，延迟加载成功后缓存结果。

        Args:
            name (str): 当前对象的名称。

        Returns:
            Type[BaseSkill] | None: 技能类，未注册或导入失败时为空。
        """
        if name in cls._skills:
            return cls._skills[name]
        if name in cls._lazy_skills:
            skill_class = cls._resolve_lazy(name)
            if skill_class:
                cls._skills[name] = skill_class
                return skill_class
        return None

    @classmethod
    def _resolve_lazy(cls, name: str) -> Type[BaseSkill] | None:
        """按 模块:类名 导入已登记技能，失败记录日志而不阻断其他技能。

        Args:
            name (str): 当前对象的名称。

        Returns:
            Type[BaseSkill] | None: 导入的技能类，加载失败时为空。
        """
        spec = cls._lazy_skills.get(name)
        if not spec:
            return None
        try:
            module_path, symbol = spec.split(":", 1)
            module = import_module(module_path)
            return getattr(module, symbol)
        except Exception as e:
            logger.error("延迟加载 Skill '%s' 失败: %s", name, e)
            return None

    @classmethod
    def list_skills(cls) -> list[str]:
        """合并两种技能注册方式的名称，供设置和 Agent 装配查询。

        Returns:
            list[str]: 排序后的去重技能名称列表。
        """
        return sorted(set(cls._skills) | set(cls._lazy_skills))

    @classmethod
    def has(cls, name: str) -> bool:
        """检查技能是否已直接或延迟注册，不触发导入。

        Args:
            name (str): 当前对象的名称。

        Returns:
            bool: 名称在任一技能注册表中时为 True。
        """
        return name in cls._skills or name in cls._lazy_skills

    @classmethod
    def clear(cls) -> None:
        """清空进程内直接和延迟技能注册，供测试或重新装配使用。"""
        cls._skills.clear()
        cls._lazy_skills.clear()


# 预注册的延迟加载能力包（在此登记）
LAZY_SKILLS: dict[str, str] = {
}

for _name, _spec in LAZY_SKILLS.items():
    SkillRegistry.register_lazy(_name, _spec)
