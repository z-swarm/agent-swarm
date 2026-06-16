"""
@module agent_swarm.skills
@brief  Skill 系统包导出 + 启动时注册内置技能

@note import 此包即触发内置 skill 注册（review/debug/...）
"""

# 触发内置技能注册（review.py 模块级会调 _register_builtin_skills）
from agent_swarm.skills import review as _review  # noqa: F401
from agent_swarm.skills.base import (
    Skill,
    SkillCategory,
    SkillRegistry,
    compose_system_prompt,
)

__all__ = [
    "Skill",
    "SkillCategory",
    "SkillRegistry",
    "compose_system_prompt",
]
