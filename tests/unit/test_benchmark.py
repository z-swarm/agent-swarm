"""
@module tests.unit.test_benchmark
@brief  Benchmark 单元测试 (DESIGN §17.5)

覆盖:
  - Benchmark.run_all smoke 模式
  - compare_baseline 报警规则
  - update_baseline 写文件
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tools.benchmark import Benchmark, BenchmarkReport, CaseSample, Regression

CASES_ROOT = Path(__file__).parent.parent / "golden" / "cases"


def test_run_all_returns_report_with_samples() -> None:
    """@brief run_all 返回带 samples 的报告"""
    bench = Benchmark(cases_root=CASES_ROOT, baseline_path=Path("/nonexistent"))
    report = bench.run_all(smoke=True)
    assert isinstance(report, BenchmarkReport)
    assert len(report.samples) >= 10
    for s in report.samples:
        assert s.case_id.startswith("G-")
        assert s.duration_seconds >= 0


def test_run_all_smoke_marks_passed() -> None:
    """@brief smoke 模式用合成 output 触发 must_find——应 passed"""
    bench = Benchmark(cases_root=CASES_ROOT, baseline_path=Path("/nonexistent"))
    report = bench.run_all(smoke=True)
    # smoke 模式用 must_find keyword 拼成 output, 全命中
    for s in report.samples:
        assert s.passed, f"{s.case_id} should pass in smoke mode"


def test_compare_baseline_warns_on_duration_regression() -> None:
    """@brief duration p50 +25% 应触发 warning (>=20%)"""
    # 临时 baseline: G-001 p50=40, 实际 smoke 报 duration=0.01 (无回归)
    # 改用 compare_baseline 直接构造 sample
    bl = {
        "G-X": {
            "duration_seconds": {"p50": 40, "p95": 80},
            "total_tokens": {"p50": 100, "p95": 150},
            "quality_score": {"min": 0.66},
        }
    }
    # 构造 sample: duration=50 → +25% → warning
    samples = [CaseSample("G-X", 50.0, 100, 0.66, True)]
    bench = Benchmark(cases_root=Path("/none"), baseline_path=Path("/none"))
    regs = bench._compare(bl, samples)
    assert any(r.metric == "duration" and r.severity == "warning" for r in regs)


def test_compare_baseline_blocks_on_severe_regression() -> None:
    """@brief duration p50 +60% 应触发 block (>=50%)"""
    bl = {"G-X": {"duration_seconds": {"p50": 100}, "total_tokens": {"p50": 100}, "quality_score": {"min": 0.5}}}
    samples = [CaseSample("G-X", 160.0, 100, 0.5, True)]   # +60%
    bench = Benchmark(cases_root=Path("/none"), baseline_path=Path("/none"))
    regs = bench._compare(bl, samples)
    assert any(r.metric == "duration" and r.severity == "block" for r in regs)


def test_compare_baseline_warns_on_quality_drop() -> None:
    """@brief quality 下降 8% 应触发 warning (>=5%)"""
    bl = {"G-X": {"duration_seconds": {"p50": 100}, "total_tokens": {"p50": 100}, "quality_score": {"min": 1.0}}}
    samples = [CaseSample("G-X", 100.0, 100, 0.92, True)]  # 1.0 -> 0.92 = -8%
    bench = Benchmark(cases_root=Path("/none"), baseline_path=Path("/none"))
    regs = bench._compare(bl, samples)
    assert any(r.metric == "quality" and r.severity == "warning" for r in regs)


def test_update_baseline_writes_yaml(tmp_path: Path) -> None:
    """@brief update_baseline 写 yaml 文件"""
    out = tmp_path / "baseline.yaml"
    bench = Benchmark(cases_root=Path("/none"), baseline_path=out)
    report = BenchmarkReport(samples=[
        CaseSample("G-A", 10.0, 100, 0.8, True),
        CaseSample("G-B", 20.0, 200, 0.6, True),
    ])
    bench.update_baseline(report, out)
    assert out.exists()
    cfg = yaml.safe_load(out.read_text())
    assert "G-A" in cfg
    assert cfg["G-A"]["quality_score"]["min"] >= 0.5


def test_load_baseline_handles_missing_file(tmp_path: Path) -> None:
    """@brief 缺失 baseline 文件——不抛, 返回空 dict"""
    bench = Benchmark(cases_root=Path("/none"), baseline_path=tmp_path / "nope.yaml")
    assert bench._load_baseline() == {}


def test_cli_smoke_runs(tmp_path: Path) -> None:
    """@brief CLI smoke 模式: python tools/benchmark.py --cases ... --baseline ..."""
    import subprocess
    import sys
    result = subprocess.run(
        [sys.executable, "tools/benchmark.py", "--cases", str(CASES_ROOT),
         "--baseline", str(tmp_path / "bl.yaml")],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0
    assert "Benchmark Report" in result.stdout
    assert "G-001" in result.stdout
