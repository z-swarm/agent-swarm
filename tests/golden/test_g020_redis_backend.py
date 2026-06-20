"""
@module tests.golden.test_g020_redis_backend
@brief  G-020 Redis 后端并发 CAS 测试
"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import yaml


def _load_run_case() -> object:
    """cases/G-020_*/run_case.py 是非数字模块名——用 importlib 加载"""
    # test file: tests/golden/test_g020_redis_backend.py
    # parents[0] = tests/golden, parents[1] = tests, parents[2] = repo
    test_path = Path(__file__).resolve()
    repo = test_path.parents[2]
    case_dir = (
        repo / "tests" / "golden" / "cases" / "G-020_redis_backend_concurrent_cas"
    )
    run_path = case_dir / "run_case.py"
    spec = importlib.util.spec_from_file_location("g020_run_case", run_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {run_path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.run_case


def test_g020_invariants() -> None:
    """
    G-020: 100 个并发 agent 抢同一 task, Redis CAS 保证 winner-takes-all
    """
    run_case = _load_run_case()
    report = asyncio.run(run_case())
    assert report["invariant_ok_winner_only"], report
    assert report["invariant_version_bumped_to_1"], report
    assert report["invariant_assigned_persisted"], report
    assert report["invariant_no_partial_state"], report
    assert report["ok_count"] == 1
    assert report["conflict_count"] == 99


def test_g020_expected_yaml_matches() -> None:
    """跑测报告与 expected.yaml 字段一致 (CI 守门用)"""
    test_path = Path(__file__).resolve()
    repo = test_path.parents[2]
    expected_path = (
        repo / "tests" / "golden" / "cases"
        / "G-020_redis_backend_concurrent_cas" / "expected.yaml"
    )
    if not expected_path.exists():
        return  # 还没跑过 run_case.py
    expected = yaml.safe_load(expected_path.read_text(encoding="utf-8"))
    assert expected["invariant_ok_winner_only"] is True
    assert expected["invariant_version_bumped_to_1"] is True
    assert expected["ok_count"] == 1
