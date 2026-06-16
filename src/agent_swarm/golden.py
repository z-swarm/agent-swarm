"""
@module agent_swarm.golden
@brief  Golden Case 验收框架（DESIGN.md §17.3）

W4 落地：
  - 加载 case 目录（input.yaml / expected.yaml / 任何输入物料）
  - 运行 swarm 收集结果 + KB stats
  - 用 expected.yaml 中的契约校验产出

expected.yaml schema（W4 子集）:
    id: G-001
    title: ...
    phase: 1
    swarm_config: input.yaml
    inputs:
      pr_diff: input_pr.diff   # 可选额外输入文件
    expected:
      must_find:
        - keyword: "SQL"
          location: "auth.py:42"   # 可选，验证文件位置
      must_not_claim:
        - keyword: "XSS"
      performance:
        max_duration_seconds: 120
        max_total_tokens: 200_000
      quality:
        min_must_find_hit_rate: 0.85   # ≥85% must_find 命中算合格
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)


@dataclass
class GoldenExpectation:
    """expected.yaml 解析后的契约"""

    case_id: str
    title: str
    phase: int
    swarm_config_path: Path
    inputs: dict[str, Path] = field(default_factory=dict)
    must_find: list[dict[str, Any]] = field(default_factory=list)
    must_not_claim: list[dict[str, Any]] = field(default_factory=list)
    performance: dict[str, Any] = field(default_factory=dict)
    quality: dict[str, Any] = field(default_factory=dict)


@dataclass
class GoldenVerdict:
    """跑完一个 case 的判定结果"""

    case_id: str
    passed: bool
    must_find_hits: list[dict[str, Any]] = field(default_factory=list)
    must_find_misses: list[dict[str, Any]] = field(default_factory=list)
    must_not_violations: list[dict[str, Any]] = field(default_factory=list)
    performance_violations: list[str] = field(default_factory=list)
    quality_score: float = 0.0
    duration_seconds: float = 0.0
    total_tokens: int = 0
    output_text: str = ""

    def summary(self) -> str:
        lines = [
            f"Case {self.case_id}: {'PASS' if self.passed else 'FAIL'}",
            f"  must_find: {len(self.must_find_hits)} hit / "
            f"{len(self.must_find_misses)} miss "
            f"(rate={self.quality_score:.2%})",
        ]
        if self.must_not_violations:
            lines.append(f"  must_not_claim violations: {len(self.must_not_violations)}")
        if self.performance_violations:
            lines.append(f"  perf violations: {self.performance_violations}")
        lines.append(
            f"  duration={self.duration_seconds:.1f}s tokens={self.total_tokens}"
        )
        return "\n".join(lines)


def load_expectation(case_dir: Path) -> GoldenExpectation:
    """从 case 目录读取 expected.yaml"""
    expected_path = case_dir / "expected.yaml"
    if not expected_path.exists():
        raise FileNotFoundError(f"expected.yaml not found in {case_dir}")
    cfg = yaml.safe_load(expected_path.read_text(encoding="utf-8"))
    if not isinstance(cfg, dict):
        raise ValueError(f"{expected_path}: root must be mapping")

    swarm_cfg = cfg.get("swarm_config", "input.yaml")
    swarm_cfg_path = (case_dir / swarm_cfg).resolve()
    if not swarm_cfg_path.exists():
        raise FileNotFoundError(f"swarm_config not found: {swarm_cfg_path}")

    inputs_raw = cfg.get("inputs") or {}
    inputs = {k: (case_dir / v).resolve() for k, v in inputs_raw.items()}

    expected = cfg.get("expected") or {}
    return GoldenExpectation(
        case_id=str(cfg["id"]),
        title=str(cfg.get("title") or cfg["id"]),
        phase=int(cfg.get("phase", 1)),
        swarm_config_path=swarm_cfg_path,
        inputs=inputs,
        must_find=list(expected.get("must_find") or []),
        must_not_claim=list(expected.get("must_not_claim") or []),
        performance=dict(expected.get("performance") or {}),
        quality=dict(expected.get("quality") or {}),
    )


def evaluate(
    expectation: GoldenExpectation,
    output_text: str,
    duration_seconds: float,
    total_tokens: int,
) -> GoldenVerdict:
    """
    根据 expectation 评估一次 swarm 运行的产出

    @param output_text 所有任务最终文本拼起来（or 单个任务的）
                       —— 关键词检查的输入

    判定规则:
      - must_find: 每条 keyword 子串大小写不敏感匹配
        - 若同时给了 location（如 "auth.py:42"），则必须同时含
      - must_not_claim: 任意 keyword 出现即违规
      - performance: max_duration_seconds / max_total_tokens
      - quality.min_must_find_hit_rate: 默认 1.0（全中）
    """
    text = output_text.lower()

    hits: list[dict[str, Any]] = []
    misses: list[dict[str, Any]] = []
    for must in expectation.must_find:
        kw = (must.get("keyword") or "").lower()
        loc = (must.get("location") or "").lower()
        kw_ok = kw in text if kw else True
        # W4-Z6: location 用边界感知匹配——避免 "auth.py" 误中 "author.py"
        # 简单规则：location 前后必须是非字母数字字符（或字符串边界）
        loc_ok = _location_in_text(loc, text) if loc else True
        if kw_ok and loc_ok:
            hits.append(must)
        else:
            misses.append(must)

    violations: list[dict[str, Any]] = []
    for must_not in expectation.must_not_claim:
        kw = (must_not.get("keyword") or "").lower()
        if kw and kw in text:
            violations.append(must_not)

    perf_violations: list[str] = []
    max_duration = expectation.performance.get("max_duration_seconds")
    if max_duration is not None and duration_seconds > max_duration:
        perf_violations.append(
            f"duration {duration_seconds:.1f}s > {max_duration}s"
        )
    max_tokens = expectation.performance.get("max_total_tokens")
    if max_tokens is not None and total_tokens > max_tokens:
        perf_violations.append(
            f"tokens {total_tokens} > {max_tokens}"
        )

    total = len(expectation.must_find)
    score = (len(hits) / total) if total > 0 else 1.0
    min_rate = expectation.quality.get("min_must_find_hit_rate", 1.0)
    quality_ok = score >= min_rate

    passed = (
        not violations
        and not perf_violations
        and quality_ok
    )

    return GoldenVerdict(
        case_id=expectation.case_id,
        passed=passed,
        must_find_hits=hits,
        must_find_misses=misses,
        must_not_violations=violations,
        performance_violations=perf_violations,
        quality_score=score,
        duration_seconds=duration_seconds,
        total_tokens=total_tokens,
        output_text=output_text,
    )


def _location_in_text(location: str, text: str) -> bool:
    """
    location（如 "auth.py" 或 "auth.py:42"）的边界感知匹配

    避免 "auth.py" 误中 "author.py"——前后字符必须是非标识符字符或边界
    """
    import re

    if not location:
        return True
    # 转义 . 和其他正则特殊字符；用 \b 或类似边界
    # 路径含 / 和 . 不被 \b 视为词边界——自己实现
    pattern = re.escape(location)
    # 前后允许：字符串开头/结尾，或非字母数字下划线
    full_pattern = rf"(?:^|[^\w]){pattern}(?:$|[^\w])"
    return re.search(full_pattern, text) is not None
