"""单元测试：Skill 系统 + 内置 review skills"""

from __future__ import annotations

import pytest

from agent_swarm.skills import SkillRegistry, compose_system_prompt
from agent_swarm.skills.base import Skill

# ---------------------------------------------------------------------------
# 内置 skill 注册（review.py import 时触发）
# ---------------------------------------------------------------------------


def test_security_skill_registered() -> None:
    s = SkillRegistry.get("code-review:security")
    assert s is not None
    assert s.category == "review"
    assert "SQL" in s.system_prompt_extension
    assert "XSS" in s.system_prompt_extension
    assert "read_file" in s.required_tools


def test_performance_skill_registered() -> None:
    s = SkillRegistry.get("code-review:performance")
    assert s is not None
    assert s.category == "review"


def test_architecture_skill_registered() -> None:
    s = SkillRegistry.get("code-review:architecture")
    assert s is not None


def test_list_ids_filter_by_category() -> None:
    review_ids = SkillRegistry.list_ids(category="review")
    assert "code-review:security" in review_ids
    assert "code-review:performance" in review_ids
    assert all(":" in i for i in review_ids)


def test_list_ids_no_filter_returns_all() -> None:
    all_ids = SkillRegistry.list_ids()
    assert len(all_ids) >= 3
    assert "code-review:security" in all_ids


def test_security_skill_metadata() -> None:
    s = SkillRegistry.get("code-review:security")
    assert s.metadata.get("checks")
    assert "SQL_INJECTION" in s.metadata["checks"]


# ---------------------------------------------------------------------------
# 注册 / 反注册
# ---------------------------------------------------------------------------


def test_register_duplicate_raises() -> None:
    """同 id 重复注册应抛"""
    s = Skill(
        id="test:dup", description="x", version="1.0",
        category="develop", system_prompt_extension="",
    )
    SkillRegistry.register(s)
    try:
        with pytest.raises(ValueError, match="already registered"):
            SkillRegistry.register(s)
    finally:
        SkillRegistry.unregister("test:dup")


def test_get_unknown_returns_none() -> None:
    assert SkillRegistry.get("ghost:skill") is None


def test_unregister_removes() -> None:
    s = Skill(
        id="test:tmp", description="x", version="1.0",
        category="ops", system_prompt_extension="",
    )
    SkillRegistry.register(s)
    assert SkillRegistry.get("test:tmp") is not None
    SkillRegistry.unregister("test:tmp")
    assert SkillRegistry.get("test:tmp") is None


def test_unregister_unknown_no_raise() -> None:
    """unregister 不存在的 skill 不应抛"""
    SkillRegistry.unregister("never-registered")


def test_list_all_returns_skill_instances() -> None:
    """list_all 应返回完整 Skill 实例（不仅 id）"""
    skills = SkillRegistry.list_all()
    assert len(skills) >= 3  # 至少 3 个内置 review skill
    assert all(isinstance(s, Skill) for s in skills)
    ids = {s.id for s in skills}
    assert "code-review:security" in ids


# ---------------------------------------------------------------------------
# compose_system_prompt
# ---------------------------------------------------------------------------


def test_compose_no_skills() -> None:
    out = compose_system_prompt(
        base_persona="be helpful",
        role="reviewer",
        agent_id="a-1",
        skills=[],
    )
    assert "be helpful" in out
    assert "reviewer" in out
    assert "a-1" in out
    assert "Skills" not in out  # 无 skill 不应有 Skills 段落


def test_compose_with_skill() -> None:
    s = SkillRegistry.get("code-review:security")
    out = compose_system_prompt(
        base_persona="careful reviewer",
        role="security expert",
        agent_id="sec-1",
        skills=[s],
    )
    assert "security expert" in out
    assert "code-review:security" in out
    assert "SQL Injection" in out
    assert "Skills" in out


def test_compose_multiple_skills() -> None:
    sec = SkillRegistry.get("code-review:security")
    perf = SkillRegistry.get("code-review:performance")
    out = compose_system_prompt(
        base_persona="reviewer",
        role="r",
        agent_id="x",
        skills=[sec, perf],
    )
    assert "code-review:security" in out
    assert "code-review:performance" in out


def test_compose_includes_tool_use_instruction() -> None:
    """无论是否有 skill，都应包含工具使用提示"""
    out = compose_system_prompt(
        base_persona="x", role="r", agent_id="a", skills=[]
    )
    assert "tools" in out.lower()


# ---------------------------------------------------------------------------
# Skill validate hooks（默认实现）
# ---------------------------------------------------------------------------


async def test_default_validate_input_returns_true() -> None:
    s = Skill(
        id="test:val1", description="", version="1.0",
        category="develop", system_prompt_extension="",
    )
    assert await s.validate_input({}) is True


async def test_default_validate_output_returns_true() -> None:
    s = Skill(
        id="test:val2", description="", version="1.0",
        category="develop", system_prompt_extension="",
    )
    assert await s.validate_output("anything") is True
