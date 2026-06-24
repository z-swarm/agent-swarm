"""单元测试：Golden Case 框架——加载 expected.yaml + evaluate"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from agent_swarm.golden import (
    GoldenExpectation,
    evaluate,
    load_expectation,
)


def _seed_case(case_dir: Path, expected: dict, swarm_cfg: dict | None = None) -> None:
    """构造 case 目录骨架"""
    case_dir.mkdir(parents=True, exist_ok=True)
    (case_dir / "expected.yaml").write_text(yaml.safe_dump(expected), encoding="utf-8")
    if swarm_cfg is None:
        swarm_cfg = {
            "name": expected.get("id", "x"),
            "agents": [{"id": "a", "role": "r", "provider": "openai", "model": "gpt-4o-mini"}],
            "tasks": [{"title": "t"}],
        }
    (case_dir / "input.yaml").write_text(yaml.safe_dump(swarm_cfg), encoding="utf-8")


# ---------------------------------------------------------------------------
# load_expectation
# ---------------------------------------------------------------------------


def test_load_expectation_minimal(tmp_path: Path) -> None:
    case = tmp_path / "G-001"
    _seed_case(case, {"id": "G-001", "title": "test", "phase": 1})

    exp = load_expectation(case)
    assert exp.case_id == "G-001"
    assert exp.title == "test"
    assert exp.phase == 1
    assert exp.swarm_config_path.name == "input.yaml"
    assert exp.must_find == []


def test_load_expectation_with_must_find(tmp_path: Path) -> None:
    case = tmp_path / "G"
    _seed_case(
        case,
        {
            "id": "G",
            "expected": {
                "must_find": [
                    {"keyword": "SQL", "location": "auth.py:42"},
                    {"keyword": "XSS"},
                ],
                "must_not_claim": [{"keyword": "false_positive"}],
            },
        },
    )
    exp = load_expectation(case)
    assert len(exp.must_find) == 2
    assert exp.must_find[0]["keyword"] == "SQL"
    assert len(exp.must_not_claim) == 1


def test_load_expectation_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="expected.yaml"):
        load_expectation(tmp_path)


def test_load_expectation_missing_swarm_config_raises(tmp_path: Path) -> None:
    case = tmp_path / "G"
    case.mkdir()
    (case / "expected.yaml").write_text(
        yaml.safe_dump({"id": "G", "swarm_config": "missing.yaml"}),
        encoding="utf-8",
    )
    with pytest.raises(FileNotFoundError, match="swarm_config"):
        load_expectation(case)


def test_load_expectation_resolves_inputs(tmp_path: Path) -> None:
    case = tmp_path / "G"
    _seed_case(case, {"id": "G", "inputs": {"diff": "pr.diff"}})
    (case / "pr.diff").write_text("diff content", encoding="utf-8")
    exp = load_expectation(case)
    assert "diff" in exp.inputs
    assert exp.inputs["diff"].name == "pr.diff"


def test_load_expectation_invalid_root_type(tmp_path: Path) -> None:
    case = tmp_path / "G"
    case.mkdir()
    (case / "expected.yaml").write_text("- not\n- mapping\n", encoding="utf-8")
    with pytest.raises(ValueError, match="mapping"):
        load_expectation(case)


# ---------------------------------------------------------------------------
# evaluate——核心判定逻辑
# ---------------------------------------------------------------------------


def _make_exp(**kwargs) -> GoldenExpectation:
    """构造一个最小可用的 GoldenExpectation"""
    return GoldenExpectation(
        case_id=kwargs.get("case_id", "X"),
        title=kwargs.get("title", "x"),
        phase=kwargs.get("phase", 1),
        swarm_config_path=Path("/tmp/x.yaml"),
        must_find=kwargs.get("must_find", []),
        must_not_claim=kwargs.get("must_not_claim", []),
        performance=kwargs.get("performance", {}),
        quality=kwargs.get("quality", {}),
    )


def test_evaluate_all_must_find_hit() -> None:
    exp = _make_exp(
        must_find=[
            {"keyword": "SQL injection"},
            {"keyword": "auth"},
        ]
    )
    v = evaluate(exp, "Found SQL injection in auth module", 1.0, 100)
    assert v.passed
    assert len(v.must_find_hits) == 2
    assert v.quality_score == 1.0


def test_evaluate_partial_hit_below_threshold() -> None:
    exp = _make_exp(
        must_find=[{"keyword": "A"}, {"keyword": "B"}, {"keyword": "C"}],
        quality={"min_must_find_hit_rate": 0.85},
    )
    v = evaluate(exp, "found A only", 1.0, 100)
    # 1/3 ≈ 0.33 < 0.85 → fail
    assert not v.passed
    assert v.quality_score == pytest.approx(1 / 3)


def test_evaluate_partial_hit_above_threshold() -> None:
    exp = _make_exp(
        must_find=[{"keyword": "A"}, {"keyword": "B"}],
        quality={"min_must_find_hit_rate": 0.5},
    )
    v = evaluate(exp, "got A here", 1.0, 100)
    assert v.passed
    assert v.quality_score == 0.5


def test_evaluate_must_find_with_location() -> None:
    """同时给 keyword + location，二者都需在输出中"""
    exp = _make_exp(
        must_find=[
            {"keyword": "SQL", "location": "auth.py:42"},
        ]
    )
    # keyword OK 但 location 缺
    v_partial = evaluate(exp, "Found SQL but somewhere", 1.0, 100)
    assert not v_partial.passed

    # 都齐
    v_full = evaluate(exp, "Found SQL in auth.py:42 line", 1.0, 100)
    assert v_full.passed


def test_evaluate_must_not_claim_violation() -> None:
    exp = _make_exp(
        must_find=[{"keyword": "real"}],
        must_not_claim=[{"keyword": "false_positive"}],
    )
    v = evaluate(exp, "real issue + false_positive claim", 1.0, 100)
    assert not v.passed
    assert len(v.must_not_violations) == 1


def test_evaluate_must_not_claim_clean_passes() -> None:
    exp = _make_exp(
        must_find=[{"keyword": "ok"}],
        must_not_claim=[{"keyword": "bad"}],
    )
    v = evaluate(exp, "ok finding", 1.0, 100)
    assert v.passed


def test_evaluate_performance_duration_violation() -> None:
    exp = _make_exp(
        must_find=[{"keyword": "x"}],
        performance={"max_duration_seconds": 5.0},
    )
    v = evaluate(exp, "x", duration_seconds=10.0, total_tokens=100)
    assert not v.passed
    assert any("duration" in p for p in v.performance_violations)


def test_evaluate_performance_tokens_violation() -> None:
    exp = _make_exp(
        must_find=[{"keyword": "x"}],
        performance={"max_total_tokens": 100},
    )
    v = evaluate(exp, "x", duration_seconds=1.0, total_tokens=500)
    assert not v.passed
    assert any("tokens" in p for p in v.performance_violations)


def test_evaluate_case_insensitive_keyword() -> None:
    exp = _make_exp(must_find=[{"keyword": "SQL Injection"}])
    v = evaluate(exp, "found sql injection here", 1.0, 100)
    assert v.passed


def test_evaluate_no_must_find_default_passes() -> None:
    """空 must_find 不应导致失败"""
    exp = _make_exp(must_find=[], must_not_claim=[])
    v = evaluate(exp, "anything", 1.0, 100)
    assert v.passed
    assert v.quality_score == 1.0


def test_evaluate_summary_format() -> None:
    """summary() 返回有可读字符串，含关键统计"""
    exp = _make_exp(must_find=[{"keyword": "x"}, {"keyword": "y"}])
    v = evaluate(exp, "got x", 2.5, 1234)
    s = v.summary()
    assert "FAIL" in s  # 1/2 命中 < 默认 100%
    assert "1234" in s


def test_evaluate_summary_includes_violations() -> None:
    """W4-ZT12 覆盖：summary() 列出 must_not / perf 违规"""
    exp = _make_exp(
        must_find=[{"keyword": "x"}],
        must_not_claim=[{"keyword": "bad"}],
        performance={"max_duration_seconds": 1.0},
    )
    v = evaluate(exp, "got x but bad", duration_seconds=10.0, total_tokens=100)
    s = v.summary()
    # 应同时含 must_not 和 perf 违规说明
    assert "must_not" in s
    assert "perf" in s


def test_evaluate_location_does_not_match_substring() -> None:
    """W4-Z6 回归：location='auth.py' 不应误中 'author.py'"""
    exp = _make_exp(must_find=[{"keyword": "vuln", "location": "auth.py"}])
    # text 含 author.py 但不含 auth.py 作为独立标记
    v = evaluate(exp, "vuln in author.py:42", 1.0, 100)
    assert not v.passed, "auth.py should not match author.py"


def test_evaluate_location_matches_at_word_boundary() -> None:
    """W4-Z6 回归：location 在合法边界应匹配"""
    exp = _make_exp(must_find=[{"keyword": "issue", "location": "auth.py"}])
    # 不同合法上下文都应命中
    for ctx in (
        "issue at auth.py:42",
        "auth.py contains issue",
        "(see auth.py) issue",
        "auth.py:5 issue",
    ):
        v = evaluate(exp, ctx, 1.0, 100)
        assert v.passed, f"location should match in: {ctx!r}"


def test_evaluate_location_with_line_number() -> None:
    """带行号的 location"""
    exp = _make_exp(must_find=[{"keyword": "bug", "location": "auth.py:42"}])
    v = evaluate(exp, "bug at auth.py:42 found", 1.0, 100)
    assert v.passed
    # 行号不同不应命中
    v_wrong = evaluate(exp, "bug at auth.py:99 found", 1.0, 100)
    assert not v_wrong.passed
