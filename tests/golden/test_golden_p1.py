"""
@module tests.golden.test_golden_p1
@brief  Phase 1 Golden Case 统一测试（G-002..G-010 + G-001）

DESIGN §17.2 W1-W6 DoD: Phase 1 P1 case 100% 通过
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from agent_swarm.golden import evaluate, load_expectation

CASES_ROOT = Path(__file__).parent / "cases"
# P1 cases: G-001 ~ G-010（10 个）
P1_CASES = sorted(
    [
        d
        for d in CASES_ROOT.iterdir()
        if d.is_dir()
        and d.name.startswith("G-0")
        # 提取数字部分（G-001_x -> 1, G-018_x -> 18）
        and 1 <= int(d.name.split("_")[0].split("-")[1]) <= 10
    ]
)


def _list_p1_cases() -> list[str]:
    return [d.name for d in P1_CASES]


@pytest.mark.parametrize("case_name", _list_p1_cases())
def test_case_expected_yaml_loads(case_name: str) -> None:
    """@brief expected.yaml 加载不报错——schema 正确"""
    case_dir = CASES_ROOT / case_name
    exp = load_expectation(case_dir)
    assert exp.case_id == case_name.split("_")[0]
    assert exp.phase == 1
    assert exp.title


def test_p1_case_count_meets_dod() -> None:
    """@brief DESIGN §17.2: 至少 10 个 P1 case (G-001..G-010)"""
    cases = _list_p1_cases()
    assert len(cases) >= 10, f"Phase 1 DoD 要求 10 个 P1 case, 当前 {len(cases)}"


def test_evaluate_with_synthetic_output() -> None:
    """@brief 评估函数对命中/不命中/性能越界都能正确判定"""
    case_dir = CASES_ROOT / "G-001_pr_security_review"
    exp = load_expectation(case_dir)
    # 包含 location: "auth.py" 满足 must_find 的 location 约束
    out = "Found SQL injection at auth.py:6, command injection, hardcoded credentials in auth.py"
    verdict = evaluate(exp, out, duration_seconds=10.0, total_tokens=1000)
    assert verdict.quality_score >= 0.66
    verdict2 = evaluate(exp, "no issues found", duration_seconds=10.0, total_tokens=1000)
    assert verdict2.quality_score == 0.0
    verdict3 = evaluate(exp, out, duration_seconds=999.0, total_tokens=1000)
    assert any("duration" in v.lower() for v in verdict3.performance_violations)


def test_all_p1_cases_load_under_1_second() -> None:
    """@brief 加载性能——避免 case 数量膨胀后启动慢"""
    t0 = time.monotonic()
    for case_name in _list_p1_cases():
        load_expectation(CASES_ROOT / case_name)
    elapsed = time.monotonic() - t0
    assert elapsed < 1.0


def test_g002_n_plus_one_underlies_review_skill() -> None:
    """@brief G-002: N+1 query 检出——expected 命中"""
    exp = load_expectation(CASES_ROOT / "G-002_n_plus_one_query")
    sample = "Found N+1 query in users endpoint using per-row select"
    verdict = evaluate(exp, sample, duration_seconds=10.0, total_tokens=1000)
    assert verdict.quality_score > 0


def test_g003_clean_pr_does_not_claim_vuln() -> None:
    """@brief G-003: 干净 PR 不误报"""
    exp = load_expectation(CASES_ROOT / "G-003_clean_pr_no_findings")
    sample = "Reviewed: no obvious issues found. Code looks clean."
    verdict = evaluate(exp, sample, duration_seconds=10.0, total_tokens=500)
    assert not verdict.must_not_violations


def test_g004_w3_resume_basic_smoke() -> None:
    """@brief G-004: W3 resume smoke"""
    exp = load_expectation(CASES_ROOT / "G-004_w3_resume")
    sample = "Session resumed. Tasks completed."
    verdict = evaluate(exp, sample, duration_seconds=5.0, total_tokens=200)
    assert not verdict.must_not_violations


def test_g005_w5_attack_blocked_no_data_leaked() -> None:
    """@brief G-005: W5 攻击拦截——must_not_claim 验证 /etc/passwd 内容未泄露"""
    exp = load_expectation(CASES_ROOT / "G-005_w5_attack_blocked")
    safe_output = "[error] policy denied: sensitive path blocked"
    verdict = evaluate(exp, safe_output, duration_seconds=3.0, total_tokens=100)
    assert not verdict.must_not_violations
    assert verdict.must_find_hits
