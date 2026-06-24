"""单元测试：channels/card_templates.py——5 个内置飞书卡片模板"""

from __future__ import annotations

import pytest

from agent_swarm.channels.card_templates import (
    TEMPLATES,
    render_adversarial_debug,
    render_card,
    render_code_review_result,
    render_confirm_dialog,
    render_swarm_status,
    render_task_progress,
)


# 通用工具
def _elements(card):
    return card["elements"]


def _has_action(card):
    return any(e.get("tag") == "action" for e in _elements(card))


def _all_text(card) -> str:
    parts: list[str] = []
    for e in _elements(card):
        if "text" in e and "content" in e["text"]:
            parts.append(e["text"]["content"])
        if "fields" in e:
            for f in e["fields"]:
                if "text" in f and "content" in f["text"]:
                    parts.append(f["text"]["content"])
        if e.get("tag") == "action":
            for a in e["actions"]:
                parts.append(a["text"]["content"])
    return " ".join(parts)


# ---------------------------------------------------------------------------
# 模板注册表
# ---------------------------------------------------------------------------


def test_templates_registry_has_five_entries() -> None:
    """5 个内置模板"""
    assert len(TEMPLATES) == 5
    for k in (
        "task_progress",
        "code_review_result",
        "adversarial_debug",
        "swarm_status",
        "confirm_dialog",
    ):
        assert k in TEMPLATES


def test_render_card_raises_on_unknown_template() -> None:
    """未知模板 → ValueError"""
    with pytest.raises(ValueError, match="unknown card template"):
        render_card("nonexistent", {})


# ---------------------------------------------------------------------------
# 1) task_progress
# ---------------------------------------------------------------------------


def test_task_progress_basic() -> None:
    card = render_task_progress(
        {
            "title": "Pipeline",
            "tasks": [
                {"id": "T1", "title": "build", "status": "completed"},
                {"id": "T2", "title": "test", "status": "in_progress"},
                {"id": "T3", "title": "deploy", "status": "failed"},
            ],
            "agent_count": 2,
        }
    )
    assert card["header"]["title"]["content"] == "Pipeline"
    txt = _all_text(card)
    assert "1/3" in txt  # completed=1
    assert "failed: 1" in txt
    assert "T1" in txt and "T2" in txt and "T3" in txt


def test_task_progress_truncates_long_list() -> None:
    """超过 10 个 task 时只显示前 10 + 省略号"""
    tasks = [{"id": f"T{i}", "title": f"t{i}", "status": "pending"} for i in range(15)]
    card = render_task_progress({"title": "Big", "tasks": tasks})
    txt = _all_text(card)
    assert "5 more" in txt


def test_task_progress_completed_all_green() -> None:
    """全 completed → 0 failed"""
    card = render_task_progress(
        {
            "tasks": [{"id": "T1", "title": "x", "status": "completed"}],
        }
    )
    txt = _all_text(card)
    assert "failed: 0" in txt


# ---------------------------------------------------------------------------
# 2) code_review_result
# ---------------------------------------------------------------------------


def test_code_review_result_severity_breakdown() -> None:
    card = render_code_review_result(
        {
            "title": "PR #1",
            "findings": [
                {"severity": "critical", "file": "a.py", "line": 1, "msg": "x"},
                {"severity": "high", "file": "b.py", "line": 2, "msg": "y"},
                {"severity": "low", "file": "c.py", "line": 3, "msg": "z"},
            ],
            "verdict": "request_changes",
        }
    )
    txt = _all_text(card)
    assert "request_changes" in txt
    assert "Critical" in txt and "1" in txt  # severity count
    assert "high" in txt
    # critical + high 应被列出
    assert "a.py:1" in txt
    assert "b.py:2" in txt
    # low 不应列出（除非显式要求）
    assert "c.py:3" not in txt


def test_code_review_result_no_high_shows_clean() -> None:
    card = render_code_review_result(
        {
            "findings": [
                {"severity": "low", "file": "a.py", "line": 1, "msg": "minor"},
            ],
        }
    )
    txt = _all_text(card)
    assert "No high-severity" in txt


# ---------------------------------------------------------------------------
# 3) adversarial_debug
# ---------------------------------------------------------------------------


def test_adversarial_debug_round_progress() -> None:
    card = render_adversarial_debug(
        {
            "title": "Debug",
            "round_no": 2,
            "max_rounds": 5,
            "survivors": ["h0", "h1"],
        }
    )
    txt = _all_text(card)
    assert "2 / 5" in txt
    assert "h0" in txt and "h1" in txt


def test_adversarial_debug_convergence_message() -> None:
    card = render_adversarial_debug(
        {
            "round_no": 3,
            "max_rounds": 5,
            "survivors": ["h0"],
            "convergence_reason": "min_survivors_reached",
        }
    )
    txt = _all_text(card)
    assert "min_survivors_reached" in txt


# ---------------------------------------------------------------------------
# 4) swarm_status
# ---------------------------------------------------------------------------


def test_swarm_status_running_info_color() -> None:
    card = render_swarm_status(
        {
            "title": "My",
            "state": "running",
            "uptime_s": 12.3,
            "tokens_used": 1000,
            "agents": [
                {"id": "a", "status": "idle", "tasks_done": 0},
                {"id": "b", "status": "busy", "tasks_done": 3},
            ],
        }
    )
    assert card["header"]["template"] == "blue"  # info
    txt = _all_text(card)
    assert "12.3s" in txt
    assert "1,000" in txt


def test_swarm_status_completed_green() -> None:
    card = render_swarm_status({"state": "completed", "agents": []})
    assert card["header"]["template"] == "green"


def test_swarm_status_failed_red() -> None:
    card = render_swarm_status({"state": "failed", "agents": []})
    assert card["header"]["template"] == "red"


def test_swarm_status_truncates_agents_list() -> None:
    agents = [{"id": f"a{i}", "status": "idle", "tasks_done": 0} for i in range(12)]
    card = render_swarm_status({"state": "running", "agents": agents})
    txt = _all_text(card)
    assert "4 more" in txt


# ---------------------------------------------------------------------------
# 5) confirm_dialog
# ---------------------------------------------------------------------------


def test_confirm_dialog_default_actions() -> None:
    """无 actions 字段 → 默认 Approve/Deny"""
    card = render_confirm_dialog({"title": "Confirm?", "message": "Are you sure?"})
    assert _has_action(card)
    txt = _all_text(card)
    assert "Are you sure?" in txt
    assert "Approve" in txt
    assert "Deny" in txt


def test_confirm_dialog_custom_actions() -> None:
    card = render_confirm_dialog(
        {
            "title": "Deploy",
            "message": "Deploy to production?",
            "actions": [
                {"text": "Yes, deploy", "value": "approve", "type": "primary"},
                {"text": "Cancel", "value": "deny", "type": "default"},
            ],
        }
    )
    txt = _all_text(card)
    assert "Yes, deploy" in txt
    assert "Cancel" in txt
    # 警告色 header
    assert card["header"]["template"] == "orange"


# ---------------------------------------------------------------------------
# 直接导出函数（向后兼容）
# ---------------------------------------------------------------------------


def test_individual_renderers_callable() -> None:
    """5 个 render_xxx 各自可直接调用"""
    assert callable(render_task_progress)
    assert callable(render_code_review_result)
    assert callable(render_adversarial_debug)
    assert callable(render_swarm_status)
    assert callable(render_confirm_dialog)
