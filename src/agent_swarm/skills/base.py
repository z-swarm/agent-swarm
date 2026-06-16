"""
@module agent_swarm.skills.base
@brief  Skill 系统基类（W4）

DESIGN.md §11：Skill 是可复用的能力模块——
  - system_prompt_extension 注入 agent 的 system prompt
  - tools 列表声明此 skill 必备的工具
  - recommended_model 提示性偏好（W4 不强制使用）
  - validate_input / validate_output 用于运行前后校验

W4 简化：
  - 只实现 prompt extension + tools 联合声明
  - validate_input / output 留接口默认 True
  - 技能加载靠注册表（SkillRegistry），yaml 中 agent.skills 字段引用 id
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal

log = logging.getLogger(__name__)


SkillCategory = Literal["review", "debug", "develop", "analyze", "ops"]


@dataclass
class Skill:
    """
    可复用能力模块——DESIGN §11.1

    @note id 推荐用 "<category>:<name>" 形式（如 "code-review:security"）
    """

    id: str
    description: str
    version: str
    category: SkillCategory

    # 注入到 agent system prompt 的扩展段落（含约束、注意事项、套路）
    system_prompt_extension: str

    # 此 skill 期望的工具 id 列表——agent 配置至少应授权这些
    required_tools: list[str] = field(default_factory=list)

    # 推荐模型（仅作展示/启发，不强制）
    recommended_model: str | None = None

    # 元数据——方便 TUI / docs 展示
    metadata: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # 校验钩子——子类可覆写（W4 默认通过）
    # ------------------------------------------------------------------
    async def validate_input(self, context: dict[str, Any]) -> bool:
        """跑 skill 前校验上下文是否完备"""
        return True

    async def validate_output(self, result: Any) -> bool:
        """跑完后校验结果格式"""
        return True


# ---------------------------------------------------------------------------
# Skill 注册表
# ---------------------------------------------------------------------------


class SkillRegistry:
    """
    全局 skill 注册表——按 id 分发

    @note 进程级单例：内置 skill 在 import 时注册；用户 skill 通过 register()
    """

    # 类级单例，进程内共享
    _instances: ClassVar[dict[str, Skill]] = {}

    @classmethod
    def register(cls, skill: Skill) -> None:
        """注册一个 skill；同 id 抛 ValueError"""
        if skill.id in cls._instances:
            raise ValueError(f"skill {skill.id!r} already registered")
        cls._instances[skill.id] = skill
        log.debug("skill.registered id=%s category=%s",
                  skill.id, skill.category)

    @classmethod
    def get(cls, skill_id: str) -> Skill | None:
        return cls._instances.get(skill_id)

    @classmethod
    def list_ids(cls, category: SkillCategory | None = None) -> list[str]:
        if category is None:
            return list(cls._instances.keys())
        return [
            sid for sid, s in cls._instances.items() if s.category == category
        ]

    @classmethod
    def list_all(cls) -> list[Skill]:
        return list(cls._instances.values())

    @classmethod
    def unregister(cls, skill_id: str) -> None:
        """测试用——清理 skill"""
        cls._instances.pop(skill_id, None)


def compose_system_prompt(
    base_persona: str,
    role: str,
    agent_id: str,
    skills: list[Skill],
) -> str:
    """
    把 base persona + 多个 skill extension 合并为完整 system prompt

    @param base_persona  agent.persona 字段
    @param role          agent.role
    @param agent_id      agent.id
    @param skills        已解析的 Skill 实例列表
    """
    parts: list[str] = [f"You are {role} (id: {agent_id})."]

    persona = (base_persona or "").strip()
    if persona:
        parts.append(persona)

    if skills:
        parts.append("# Skills")
        for s in skills:
            block = [f"## {s.id} (v{s.version})", s.description.strip()]
            ext = s.system_prompt_extension.strip()
            if ext:
                block.append(ext)
            parts.append("\n\n".join(block))

    parts.append(
        "Use the provided tools to gather information and collaborate "
        "with other agents when needed. "
        "When you have completed the task, provide your final answer "
        "without calling any more tools."
    )
    return "\n\n".join(parts)
