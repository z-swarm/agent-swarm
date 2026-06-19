"""
@file tools/agent_review.py
@brief  W13 Dogfooding 工具——用 agent-swarm 自审本项目 PR

DESIGN §15 Phase 2 末期 Dogfooding：
  - 拉取 PR diff（git diff main..HEAD 或 gh pr diff）
  - 启动一个 Reviewer Swarm：
    * Plan agent (plan_only) 拆解 PR 涉及的文件 + 风险维度
    * 3 个 plan_only Judge 跑 AdversarialVerifier
    * 注入 code-review:security skill
  - 输出结构化 ReviewReport：
    * verdict: approve / request_changes / comment
    * findings: [{severity, file, line, category, description}]
    * root_causes: 对抗式分析得到的根因列表
    * summary: 一句话总结

@note  W13 落地策略：
  - 完整版：gh CLI 拉取 + 真实 Swarm 跑（需 OPENAI_API_KEY）
  - 简化版（默认）：用确定性 JudgeFn 跑 AdversarialVerifier 在静态 diff 上
  - 两种模式靠 --mode=simple|full 切换
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


REPO = Path(__file__).resolve().parent.parent
# 测试 / 多 repo 场景可通过 env 覆盖
REPO = Path(__import__("os").environ.get("AGENT_REVIEW_REPO", str(REPO)))


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class ReviewFinding:
    """单条审查发现"""

    severity: str  # CRITICAL / HIGH / MEDIUM / LOW
    file: str
    line: int
    category: str  # SQL_INJECTION / XSS / AUTH / PATH_TRAVERSAL / CMD_INJECTION / DATA_EXPOSURE / OTHER
    description: str


@dataclass
class ReviewReport:
    """PR 审查结果"""

    pr_ref: str
    verdict: str  # approve / request_changes / comment
    findings: list[ReviewFinding] = field(default_factory=list)
    root_causes: list[str] = field(default_factory=list)
    summary: str = ""
    confidence: float = 0.0
    files_changed: int = 0
    lines_changed: int = 0


# ---------------------------------------------------------------------------
# 1) 拉取 PR diff
# ---------------------------------------------------------------------------


def get_pr_diff(pr_ref: str = "main..HEAD") -> tuple[str, int, int]:
    """
    拉取 PR diff

    @param pr_ref  git diff range（"main..HEAD"）或 PR 编号（"123"）
    @return (diff_text, files_changed, lines_changed)
    @note  使用 git diff 简化；远期接 gh CLI 拉 PR 数据
    @note  lines_changed = 净增行 (added - deleted),与 git diff --numstat 一致
    """
    try:
        result = subprocess.run(
            ["git", "diff", pr_ref, "--unified=3", "--stat"],
            cwd=REPO, capture_output=True, text=True, timeout=30,
        )
        stat = result.stdout
    except Exception as exc:  # noqa: BLE001
        print(f"[error] git diff failed: {exc}", file=sys.stderr)
        return "", 0, 0
    # 拉完整 diff
    try:
        result = subprocess.run(
            ["git", "diff", pr_ref, "--unified=3"],
            cwd=REPO, capture_output=True, text=True, timeout=30,
        )
        diff = result.stdout
    except Exception:  # noqa: BLE001
        diff = ""
    # 用 --numstat 准确统计 added / deleted (M3 修复: 旧版只数 +, 不数 -)
    files = 0
    added = 0
    deleted = 0
    try:
        result = subprocess.run(
            ["git", "diff", pr_ref, "--numstat"],
            cwd=REPO, capture_output=True, text=True, timeout=30,
        )
        for line in result.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) >= 3:
                # 二进制文件是 "-\t-\tfile" → 跳过
                if parts[0] == "-" or parts[1] == "-":
                    files += 1
                    continue
                try:
                    a = int(parts[0])
                    d = int(parts[1])
                except ValueError:
                    continue
                added += a
                deleted += d
                files += 1
    except Exception:  # noqa: BLE001
        pass
    # 净增行:added - deleted(可能为负,但 git --numstat 给出总变更量更直观)
    # 这里用 added + deleted 表示"变更量",避免负数混淆 reviewer
    lines = added + deleted
    return diff, files, lines


# ---------------------------------------------------------------------------
# 2) 静态安全扫描（不依赖 LLM）
# ---------------------------------------------------------------------------


# 文件扩展名白名单——只对源码做静态扫描，避免 .md/.json/.yaml/.txt 等误报
_SOURCE_EXTENSIONS: frozenset[str] = frozenset({
    ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".go", ".rs", ".java", ".kt", ".rb", ".php", ".c", ".cpp", ".h", ".hpp",
    ".sh", ".bash", ".zsh",
})

# 路径黑名单——第三方/构建产物不应被扫
_PATH_SKIP_SUBSTRINGS: tuple[str, ...] = (
    ".venv/", "venv/", "node_modules/", "vendor/",
    ".git/", "dist/", "build/", "__pycache__/", ".pytest_cache/",
    ".mypy_cache/", ".ruff_cache/", ".coverage", "site-packages/",
)


def _is_source_file(path: str) -> bool:
    """
    @brief 判定是否值得扫的源码文件

    @param path  git diff 中的文件路径
    @return True 表示做静态扫描；False 表示跳过
    """
    if not path or path == "/dev/null":
        return False
    # 路径黑名单
    for skip in _PATH_SKIP_SUBSTRINGS:
        if skip in path:
            return False
    # 扩展名白名单
    p = path.lower()
    for ext in _SOURCE_EXTENSIONS:
        if p.endswith(ext):
            return True
    return False


# 简单规则——只做关键字 + 模式匹配，捕获高置信度安全问题
_RULES: list[dict[str, Any]] = [
    {
        "category": "SECRET_LEAK",
        "severity": "CRITICAL",
        # 匹配明文密钥赋值；跳过 SecretManager 引用 (${VAR}) 和测试用 placeholder
        "pattern": re.compile(
            r'(?i)(api[_-]?key|secret|token|password|passwd)\s*=\s*["\'](?!\$\{)[^"\']{8,}["\']'
        ),
        "description": "可能硬编码密钥/凭证——应使用 SecretManager 引用 (${VAR})",
    },
    {
        "category": "CMD_INJECTION",
        "severity": "HIGH",
        "pattern": re.compile(r"subprocess\.(?:run|call|Popen)\([^)]*shell\s*=\s*True"),
        "description": "subprocess shell=True — 存在命令注入风险（DESIGN §8.2）",
    },
    {
        "category": "PATH_TRAVERSAL",
        "severity": "HIGH",
        # M1 修复: 检测不可信输入拼接到 open() 的路径参数
        # 旧版 r"open\(\s*[^)]*(\+\s*[a-zA-Z_])" 误报率高
        # 新版:要求拼接的是真正的不可信源 (user_input/request./input()/argv[]/args[]/.params[])
        "pattern": re.compile(
            r"open\(\s*[^)]*"
            r"(?:\+\s*(?:user_?input|request\.|input\(|argv\[|args\[|\.params\[))"
        ),
        "description": "open() 拼接不可信输入 — path traversal 风险（DESIGN §8.2）",
    },
    {
        "category": "EVAL",
        "severity": "HIGH",
        # 收紧：要求前一个字符不是 [a-zA-Z0-9_."'] —— 排除方法调用(self.eval)
        # 词边界 \b 已排除 evaluate / developer / execution 等
        # 已知 limitation：字符串字面量 "use eval(" 仍可能误报；用 _line_is_string_only 二次过滤
        "pattern": re.compile(
            r'(?<![a-zA-Z0-9_."\'])\b(eval|exec)\s*\('
        ),
        "description": "eval/exec 使用 — 不安全（DESIGN §8.2）",
    },
    {
        "category": "SQL_INJECTION",
        "severity": "HIGH",
        "pattern": re.compile(
            r'(SELECT|INSERT|UPDATE|DELETE).*["\'].*%[sd]|f["\']SELECT|f["\']INSERT'
        ),
        "description": "SQL 字符串拼接 — 应使用参数化查询",
    },
    {
        "category": "DATA_EXPOSURE",
        "severity": "MEDIUM",
        "pattern": re.compile(r"print\([^)]*password|print\([^)]*token|log\.[a-z]+\([^)]*secret"),
        "description": "日志/print 可能泄露敏感信息",
    },
    {
        "category": "WEAK_HASH",
        "severity": "MEDIUM",
        # M2 修复: 仅在 security 上下文中才报
        # md5/sha1 常见非安全用途:fingerprint / cache / etag / idempotency
        # 启发式:排除调用附近的非安全关键字(negative lookahead)
        "pattern": re.compile(
            r"hashlib\.(md5|sha1)\b(?![^()\n]{0,80}"
            r"(?:\b(?:fingerprint|content[_-]?hash|etag|cache[_-]?key|"
            r"idempoten|non[_-]?crypto|non[_-]?security)\b))"
        ),
        "description": "弱哈希算法（MD5/SHA1）— 密码/签名场景应使用 SHA-256+",
    },
]


def _is_non_security_hash_use(line: str) -> bool:
    """
    @brief M2 启发式: 判断 hashlib.md5/sha1 调用是否在非安全上下文
    @param line  diff 中的 + 行(不含前缀 +)
    @return True 表示非安全用途(fingerprint/cache/etag/idempotency),应忽略
    """
    p = line.lower()
    non_security_keywords = (
        "fingerprint", "content_hash", "contenthash",
        "etag", "cache_key", "cachekey",
        "idempoten", "non_crypto", "non_security", "noncrypto",
        "checksum",  # 注意:checksum 不一定安全,但常见用于文件完整性
    )
    return any(kw in p for kw in non_security_keywords)


def _line_is_string_literal(line: str, match_start: int) -> bool:
    """
    @brief 启发式:判断 eval/exec 调用是否落在字符串字面量里

    @param line  diff 中的 + 行(不含前缀 +)
    @param match_start  eval/exec 关键字在 line 里的下标
    @return True 表示这次匹配是字符串内容,应忽略

    启发式:match 之前同一行内未配对引号数=奇数 ⇒ eval 在字符串里
    @note 已知 limitation:f-string 内 `f"{eval(x)}"` 中的 eval 会被误跳;
         要彻底解决需 AST 分析,regex 不可达。
    """
    prefix = line[:match_start]
    n_dq = len(re.findall(r'(?<!\\)"', prefix))
    n_sq = len(re.findall(r"(?<!\\)'", prefix))
    # 未配对引号为奇数 ⇒ 当前位置在字符串里
    if n_dq % 2 == 1 or n_sq % 2 == 1:
        return True
    return False


def static_security_scan(diff: str) -> list[ReviewFinding]:
    """
    静态安全扫描——纯规则匹配，不依赖 LLM

    @brief 规则应用范围：
      - 只扫描源码扩展名（.py/.js/.ts/.go/.rs/.java 等）——见 _SOURCE_EXTENSIONS
      - 跳过 .venv/ / node_modules/ / vendor/ / .git/ / 缓存目录
      - 删除行（-）不查；只查新增行（+）
    """
    findings: list[ReviewFinding] = []
    current_file = "?"
    current_line = 0
    scan_enabled = False  # 是否在源码文件内
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            current_file = line[len("+++ b/"):]
            scan_enabled = _is_source_file(current_file)
        elif line.startswith("@@"):
            m = re.search(r"\+(\d+)", line)
            if m:
                current_line = int(m.group(1)) - 1
        elif scan_enabled and line.startswith("+") and not line.startswith("+++"):
            current_line += 1
            added = line[1:]
            for rule in _RULES:
                m = rule["pattern"].search(added)
                if m is None:
                    continue
                # EVAL 规则二次过滤:跳过字符串字面量里的 eval
                if rule["category"] == "EVAL" and _line_is_string_literal(added, m.start()):
                    continue
                # M2 规则二次过滤:跳过非安全用途的 md5/sha1 (fingerprint/cache/etag)
                if rule["category"] == "WEAK_HASH" and _is_non_security_hash_use(added):
                    continue
                findings.append(ReviewFinding(
                    severity=rule["severity"],
                    file=current_file,
                    line=current_line,
                    category=rule["category"],
                    description=rule["description"],
                ))
    return findings


# ---------------------------------------------------------------------------
# 3) AdversarialVerifier — 简单模式（确定性 JudgeFn）
# ---------------------------------------------------------------------------


async def _deterministic_judge(agent: Any, hypothesis_id: str, round_no: int) -> Any:
    """
    简单 JudgeFn：对每个假设做一次判定
    @note W13 简化：用静态扫描结果做决定；完整版接 LLM
    """
    from agent_swarm.core.types import Judgement, Stance

    # 假设 id 形如 "h0" / "h1" / ... → 找 diff 中是否有该文件路径
    # 这里 demo: 全部 SUPPORT（让 AdversarialVerifier 能跑完整流程）
    return Judgement(
        agent_id=agent.id if hasattr(agent, "id") else "judge",
        hypothesis_id=hypothesis_id,
        round_no=round_no,
        stance=Stance.SUPPORT,
        confidence=0.85,
        evidence=[],
        reasoning="W13 简单模式：默认 SUPPORT；远期接 LLM 真实判定",
    )


# ---------------------------------------------------------------------------
# 4) 主流程
# ---------------------------------------------------------------------------


def run_simple_review(pr_ref: str) -> ReviewReport:
    """W13 简化模式：git diff + 静态扫描 + 输出报告"""
    diff, files, lines = get_pr_diff(pr_ref)
    findings = static_security_scan(diff)

    # 判定 verdict
    has_critical = any(f.severity == "CRITICAL" for f in findings)
    has_high = any(f.severity == "HIGH" for f in findings)
    if has_critical:
        verdict = "request_changes"
    elif has_high:
        verdict = "request_changes"
    elif findings:
        verdict = "comment"
    else:
        verdict = "approve"

    # 摘要
    by_sev: dict[str, int] = {}
    for f in findings:
        by_sev[f.severity] = by_sev.get(f.severity, 0) + 1
    parts = [f"{v}×{k}" for k, v in by_sev.items()]
    summary = f"{verdict} | {files} files / {lines} lines | findings: " + (
        ", ".join(parts) if parts else "无"
    )

    return ReviewReport(
        pr_ref=pr_ref,
        verdict=verdict,
        findings=findings,
        root_causes=[],  # 简单模式不跑对抗式
        summary=summary,
        confidence=0.95 if not findings else 0.85,
        files_changed=files,
        lines_changed=lines,
    )


async def run_full_review(pr_ref: str) -> ReviewReport:
    """W13 完整模式:AdversarialVerifier 跑 3 judges × N 假设(L2/L3 修复)

    @note 当前为占位 — 需要 OPENAI_API_KEY 或 ANTHROPIC_API_KEY 来驱动
          真实 LLM judge 跑对抗式判定。
    @note L2/L3 修复:占位明示 + 缺 API key 时 fail-fast
    @todo W14+: 实现 llm_judge factory + 接入 AdversarialVerifier.verify()
    """
    import os
    if not (os.environ.get("OPENAI_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")):
        # L2/L3 修复: 显式 fail-fast,避免静默退化为 simple
        raise RuntimeError(
            "run_full_review 需要 LLM API key;"
            " 请设置 OPENAI_API_KEY 或 ANTHROPIC_API_KEY 环境变量"
        )
    # 临时回退到 simple 模式(W14+ 替换为 AdversarialVerifier.verify)
    print(
        "[W13] full mode 占位, 回退到 simple 模式; "
        "W14+ 将接入真实 LLM judge",
        file=sys.stderr,
    )
    return run_simple_review(pr_ref)


def print_report(report: ReviewReport) -> None:
    """打印结构化报告（人类可读 + JSON）"""
    print("=" * 60)
    print(f"PR Review: {report.pr_ref}")
    print("=" * 60)
    print(f"verdict:  {report.verdict}")
    print(f"summary:  {report.summary}")
    print(f"changes:  {report.files_changed} files, {report.lines_changed} lines")
    print(f"confidence: {report.confidence}")
    print()
    if not report.findings:
        print("✅ 无安全问题")
    else:
        print(f"findings ({len(report.findings)}):")
        for f in report.findings:
            print(f"  - [{f.severity}] {f.file}:{f.line} {f.category}: {f.description}")
    print()
    if report.root_causes:
        print("root_causes:")
        for rc in report.root_causes:
            print(f"  - {rc}")
    # JSON 输出
    print()
    print("--- JSON ---")
    print(json.dumps(asdict(report), ensure_ascii=False, indent=2))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="W13 Dogfooding — agent-swarm PR 自动审查工具"
    )
    parser.add_argument(
        "--pr", default="main..HEAD",
        help="git diff 范围（默认 main..HEAD）或 PR 编号",
    )
    parser.add_argument(
        "--mode", choices=["simple", "full"], default="simple",
        help="运行模式：simple=静态规则；full=LLM + 对抗式（需 API key）",
    )
    parser.add_argument(
        "--output", choices=["text", "json"], default="text",
        help="输出格式",
    )
    args = parser.parse_args()

    if args.mode == "full":
        report = asyncio.run(run_full_review(args.pr))
    else:
        report = run_simple_review(args.pr)

    if args.output == "json":
        print(json.dumps(asdict(report), ensure_ascii=False, indent=2))
    else:
        print_report(report)

    # exit code 反映 verdict
    if report.verdict == "request_changes":
        sys.exit(1)
    elif report.verdict == "comment":
        sys.exit(0)
    else:
        sys.exit(0)


if __name__ == "__main__":
    main()
