"""
@module tools.benchmark
@brief  Performance/Quality Baseline Runner (DESIGN §17.5)

W6 阶段（Phase 1）:
  - Benchmark 类: 跑全部 P1 Golden Case 收集 duration / tokens / quality
  - compare_baseline(): 与 baseline.yaml 比对, 超过阈值报 Regression
  - update_baseline(): 人工确认后才更新基线 (防 LLM 随机波动)

数据流:
  1) run_all() → 跑 cases/G-XXX_*/ → 收集样本
  2) compare_baseline(threshold_pct) → 读 baseline.yaml → 计算 p50/p95 → 比对
  3) 报警: 警告阈值 (p95+20%, quality-5%) / 阻塞阈值 (p95+50%, quality-15%)

@note Phase 1 单租户 local 模式; 真实 LLM nightly (--llm-real) 在 Phase 2+
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


@dataclass
class CaseSample:
    """@brief 单次 case 跑的样本"""
    case_id: str
    duration_seconds: float
    total_tokens: int
    quality_score: float
    passed: bool


@dataclass
class Regression:
    """@brief 单条劣化告警"""
    case_id: str
    metric: str            # "duration" / "tokens" / "quality"
    baseline: float
    actual: float
    pct_change: float      # 正 = 劣化 (duration↑, quality↓ 用 sign)
    severity: str          # "warning" / "block"


@dataclass
class BenchmarkReport:
    """@brief 一次 run_all 的报告"""
    samples: list[CaseSample]
    regressions: list[Regression] = field(default_factory=list)
    duration_seconds: float = 0.0

    def summary(self) -> str:
        lines = [
            f"Benchmark Report: {len(self.samples)} cases, {len(self.regressions)} regressions",
        ]
        for s in self.samples:
            mark = "✓" if s.passed else "✗"
            lines.append(
                f"  {mark} {s.case_id}: {s.duration_seconds:.1f}s "
                f"tokens={s.total_tokens} quality={s.quality_score:.2%}"
            )
        if self.regressions:
            lines.append("\nRegressions:")
            for r in self.regressions:
                lines.append(
                    f"  [{r.severity}] {r.case_id}.{r.metric}: "
                    f"{r.baseline:.2f} → {r.actual:.2f} ({r.pct_change:+.1%})"
                )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Benchmark Runner
# ---------------------------------------------------------------------------


class Benchmark:
    """
    @brief 性能/质量基线运行器

    @note W6 简化: 不真跑 swarm, 而是跑 Golden Case 的 expected.yaml 加载测试
          + 用一份合成 sample 数据。Phase 2+ 接入 --llm-real 跑真实 case。
    """

    # 报警阈值 (DESIGN §17.5)
    WARNING_PCT = 20.0
    BLOCK_PCT = 50.0
    QUALITY_WARNING_PCT = 5.0
    QUALITY_BLOCK_PCT = 15.0

    def __init__(self, cases_root: Path, baseline_path: Path) -> None:
        """
        @param cases_root   tests/golden/cases/
        @param baseline_path  tests/golden/baseline.yaml
        """
        self.cases_root = cases_root
        self.baseline_path = baseline_path

    def run_all(self, smoke: bool = True) -> BenchmarkReport:
        """
        @brief 跑全部 P1 Golden Case 收集样本

        @param smoke  True 时只跑 expected.yaml 加载 (避免重 e2e)
                      False 时跑真实 case (Phase 2+)
        @return BenchmarkReport 含 samples + regressions
        """
        t0 = time.monotonic()
        samples: list[CaseSample] = []
        if not self.cases_root.exists():
            return BenchmarkReport(samples=[], duration_seconds=time.monotonic() - t0)
        for case_dir in sorted(self.cases_root.iterdir()):
            if not case_dir.is_dir() or not case_dir.name.startswith("G-"):
                continue
            sample = self._run_one(case_dir, smoke=smoke)
            samples.append(sample)
        report = BenchmarkReport(
            samples=samples, duration_seconds=time.monotonic() - t0
        )
        # 与 baseline 比对
        baseline = self._load_baseline()
        if baseline:
            report.regressions = self._compare(baseline, samples)
        return report

    def _run_one(self, case_dir: Path, smoke: bool) -> CaseSample:
        """
        @brief 跑单 case——smoke 模式只加载 + 评估 known good output
        """
        from agent_swarm.golden import evaluate, load_expectation

        exp = load_expectation(case_dir)
        # smoke 模式: 用 "全命中" 的合成 output 验证框架
        if smoke:
            parts: list[str] = []
            for m in exp.must_find:
                parts.append(m.get("keyword", ""))
                loc = m.get("location", "")
                if loc:
                    parts.append(loc)  # location 也塞进 output, 满足约束
            output = " ".join(parts) if parts else "smoke"
            duration = 0.01
            tokens = 100
        else:
            output = ""
            duration = 0.0
            tokens = 0
        verdict = evaluate(exp, output, duration_seconds=duration, total_tokens=tokens)
        return CaseSample(
            case_id=exp.case_id,
            duration_seconds=duration,
            total_tokens=tokens,
            quality_score=verdict.quality_score,
            passed=verdict.passed,
        )

    # ------------------------------------------------------------------
    # Baseline 比对
    # ------------------------------------------------------------------

    def _load_baseline(self) -> dict:
        if not self.baseline_path.exists():
            return {}
        cfg = yaml.safe_load(self.baseline_path.read_text(encoding="utf-8")) or {}
        return cfg if isinstance(cfg, dict) else {}

    def compare_baseline(
        self,
        threshold_pct: float = WARNING_PCT,
    ) -> list[Regression]:
        """@brief 跑一次 + 比对 baseline, 返回 Regression 列表"""
        report = self.run_all(smoke=True)
        return report.regressions

    def _compare(
        self, baseline: dict, samples: list[CaseSample]
    ) -> list[Regression]:
        regressions: list[Regression] = []
        for sample in samples:
            bl = baseline.get(sample.case_id)
            if not isinstance(bl, dict):
                continue
            # duration: p50/p95 阈值
            dur = bl.get("duration_seconds") or {}
            if "p50" in dur and sample.duration_seconds > 0:
                base_p50 = float(dur["p50"])
                pct = (sample.duration_seconds - base_p50) / base_p50 * 100
                sev = self._severity(pct, higher_is_worse=True)
                if sev:
                    regressions.append(Regression(
                        case_id=sample.case_id,
                        metric="duration",
                        baseline=base_p50,
                        actual=sample.duration_seconds,
                        pct_change=pct / 100,
                        severity=sev,
                    ))
            # tokens
            tok = bl.get("total_tokens") or {}
            if "p50" in tok and sample.total_tokens > 0:
                base_p50 = float(tok["p50"])
                pct = (sample.total_tokens - base_p50) / base_p50 * 100
                sev = self._severity(pct, higher_is_worse=True)
                if sev:
                    regressions.append(Regression(
                        case_id=sample.case_id,
                        metric="tokens",
                        baseline=base_p50,
                        actual=float(sample.total_tokens),
                        pct_change=pct / 100,
                        severity=sev,
                    ))
            # quality: 降低是劣化——用更敏感的 QUALITY_WARNING_PCT (5/15)
            qual = bl.get("quality_score") or {}
            if "min" in qual:
                base_min = float(qual["min"])
                pct = (base_min - sample.quality_score) / base_min * 100
                sev = self._severity_quality(pct)
                if sev:
                    regressions.append(Regression(
                        case_id=sample.case_id,
                        metric="quality",
                        baseline=base_min,
                        actual=sample.quality_score,
                        pct_change=-pct / 100,
                        severity=sev,
                    ))
        return regressions

    def _severity(self, pct_change: float, higher_is_worse: bool) -> str | None:
        """@brief duration/tokens 严重度——基于 WARNING_PCT / BLOCK_PCT"""
        if pct_change >= self.BLOCK_PCT:
            return "block"
        if pct_change >= self.WARNING_PCT:
            return "warning"
        return None

    def _severity_quality(self, pct_change: float) -> str | None:
        """@brief quality 严重度——更敏感的 5% / 15% 阈值"""
        if pct_change >= self.QUALITY_BLOCK_PCT:
            return "block"
        if pct_change >= self.QUALITY_WARNING_PCT:
            return "warning"
        return None

    # ------------------------------------------------------------------
    # 写 baseline (人工确认)
    # ------------------------------------------------------------------

    def update_baseline(self, report: BenchmarkReport, output_path: Path) -> None:
        """
        @brief 写 baseline.yaml——DESIGN 要求人工确认后才调 (防 LLM 波动污染)

        @param report      run_all 产出
        @param output_path baseline.yaml 路径
        """
        baseline: dict = {}
        for s in report.samples:
            baseline[s.case_id] = {
                "duration_seconds": {"p50": int(s.duration_seconds), "p95": int(s.duration_seconds * 1.2)},
                "total_tokens": {"p50": s.total_tokens, "p95": int(s.total_tokens * 1.2)},
                "quality_score": {"min": max(0.5, s.quality_score)},
                "last_updated": time.strftime("%Y-%m-%d"),
                "last_updated_by": "@benchmark-runner",
            }
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            yaml.safe_dump(baseline, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="agent-swarm benchmark runner")
    parser.add_argument(
        "--cases", type=Path, default=Path("tests/golden/cases"),
        help="Golden case 根目录",
    )
    parser.add_argument(
        "--baseline", type=Path, default=Path("tests/golden/baseline.yaml"),
        help="Baseline 文件路径",
    )
    parser.add_argument(
        "--smoke", action="store_true", default=True,
        help="smoke 模式——只跑 expected.yaml 加载 + 评估框架 (Phase 1)",
    )
    parser.add_argument(
        "--no-smoke", dest="smoke", action="store_false",
        help="真实跑 case (Phase 2+, 需要 --llm-real)",
    )
    parser.add_argument(
        "--compare-baseline", action="store_true",
        help="跑完后与 baseline 比对, 输出 regressions",
    )
    parser.add_argument(
        "--update-baseline", type=Path, default=None, metavar="PATH",
        help="写新 baseline 到指定路径 (人工确认后调)",
    )
    args = parser.parse_args()

    bench = Benchmark(cases_root=args.cases, baseline_path=args.baseline)
    report = bench.run_all(smoke=args.smoke)
    print(report.summary())

    if args.update_baseline:
        bench.update_baseline(report, args.update_baseline)
        print(f"\nBaseline 写入: {args.update_baseline}")

    if args.compare_baseline and report.regressions:
        return 1  # CI 阻塞
    return 0


if __name__ == "__main__":
    sys.exit(main())
