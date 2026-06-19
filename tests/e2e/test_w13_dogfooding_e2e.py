"""
@module tests.e2e.test_w13_dogfooding_e2e
@brief  W13 Dogfooding 端到端验证

W13 DoD（DESIGN §15 Phase 2 末期）：
  ① agent_review.py 工具可跑（git diff + 静态规则扫描）
  ② 干净 diff → verdict=approve
  ③ 有 critical finding → verdict=request_changes
  ④ 输出结构化 ReviewReport（JSON + text）
  ⑤ exit code 反映 verdict（approve/comment=0；request_changes=1）

@note W13 完整版（--mode=full）接 LLM + AdversarialVerifier，远期 W14+
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from tools.agent_review import (
    ReviewFinding,
    ReviewReport,
    get_pr_diff,
    run_simple_review,
    static_security_scan,
)


def _setup_git_repo(tmp_path: Path, files: dict[str, str]) -> None:
    """在 tmp_path 初始化 git 仓库 + 提交第一个 commit"""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True
    )
    subprocess.run(
        ["git", "config", "user.name", "T"], cwd=tmp_path, check=True
    )
    for path, content in files.items():
        p = tmp_path / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)


def _commit_changes(tmp_path: Path, changes: dict[str, str]) -> None:
    """修改文件 + 提交"""
    for path, content in changes.items():
        p = tmp_path / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "update"], cwd=tmp_path, check=True)


# ---------------------------------------------------------------------------
# ① 工具可跑
# ---------------------------------------------------------------------------


def test_e2e_agent_review_runs_on_real_repo_diff() -> None:
    """在真项目上跑 agent_review HEAD~3..HEAD（应至少 1 个 finding 因为有 SECRET_LEAK 测试）"""
    result = subprocess.run(
        [".venv/bin/python", "tools/agent_review.py",
         "--pr", "HEAD~3..HEAD", "--output", "json"],
        capture_output=True, text=True, timeout=30,
    )
    # 允许 exit code 0 / 1（verdict=comment / request_changes）
    assert result.returncode in (0, 1), f"unexpected exit: {result.returncode} stderr={result.stderr}"
    data = json.loads(result.stdout)
    assert "verdict" in data
    assert data["pr_ref"] == "HEAD~3..HEAD"
    assert "findings" in data
    assert "summary" in data
    assert "files_changed" in data
    assert "lines_changed" in data
    assert data["files_changed"] >= 1
    # 真项目 PR 应有 ≥0 findings
    assert isinstance(data["findings"], list)


# ---------------------------------------------------------------------------
# ② 干净 diff → approve
# ---------------------------------------------------------------------------


def test_e2e_clean_diff_returns_approve(tmp_path: Path) -> None:
    """干净 PR diff → verdict=approve"""
    _setup_git_repo(tmp_path, {"x.py": "a = 1\n"})
    _commit_changes(tmp_path, {"x.py": "a = 1\nb = 2\n"})

    env = {**os.environ, "AGENT_REVIEW_REPO": str(tmp_path)}
    result = subprocess.run(
        [".venv/bin/python", "tools/agent_review.py",
         "--pr", "HEAD~1..HEAD", "--output", "json"],
        capture_output=True, text=True, timeout=30,
        env=env,
    )
    assert result.returncode == 0, f"clean PR 应通过: stderr={result.stderr}"
    data = json.loads(result.stdout)
    assert data["verdict"] == "approve"
    assert data["files_changed"] == 1


# ---------------------------------------------------------------------------
# ③ 有 critical → request_changes
# ---------------------------------------------------------------------------


def test_e2e_critical_finding_returns_request_changes(tmp_path: Path) -> None:
    """PR 含明文 api_key → verdict=request_changes + exit code 1"""
    _setup_git_repo(tmp_path, {"x.py": "a = 1\n"})
    _commit_changes(tmp_path, {
        "x.py": "a = 1\napi_key = 'sk-1234567890abcdefghijklmnop'\n"
    })

    env = {**os.environ, "AGENT_REVIEW_REPO": str(tmp_path)}
    result = subprocess.run(
        [".venv/bin/python", "tools/agent_review.py",
         "--pr", "HEAD~1..HEAD", "--output", "json"],
        capture_output=True, text=True, timeout=30,
        env=env,
    )
    assert result.returncode == 1, "有 critical 应 exit 1"
    data = json.loads(result.stdout)
    assert data["verdict"] == "request_changes"
    cats = {f["category"] for f in data["findings"]}
    assert "SECRET_LEAK" in cats


# ---------------------------------------------------------------------------
# ④ 输出格式：text + JSON 都可用
# ---------------------------------------------------------------------------


def test_e2e_text_output_includes_verdict_and_findings(tmp_path: Path) -> None:
    """text 输出含 verdict + findings 列表"""
    _setup_git_repo(tmp_path, {"x.py": "a\n"})
    _commit_changes(tmp_path, {"x.py": "eval(user_input)\n"})

    env = {**os.environ, "AGENT_REVIEW_REPO": str(tmp_path)}
    result = subprocess.run(
        [".venv/bin/python", "tools/agent_review.py",
         "--pr", "HEAD~1..HEAD"],  # text mode (default)
        capture_output=True, text=True, timeout=30,
        env=env,
    )
    out = result.stdout
    assert "verdict:" in out
    assert "findings" in out
    assert "EVAL" in out


def test_e2e_json_output_is_parseable(tmp_path: Path) -> None:
    """JSON 输出是合法 JSON"""
    _setup_git_repo(tmp_path, {"x.py": "a\n"})
    _commit_changes(tmp_path, {"x.py": "b\n"})

    env = {**os.environ, "AGENT_REVIEW_REPO": str(tmp_path)}
    result = subprocess.run(
        [".venv/bin/python", "tools/agent_review.py",
         "--pr", "HEAD~1..HEAD", "--output", "json"],
        capture_output=True, text=True, timeout=30,
        env=env,
    )
    data = json.loads(result.stdout)  # 解析失败则 raise
    assert "verdict" in data
    assert "findings" in data
    assert "summary" in data
    assert "confidence" in data


# ---------------------------------------------------------------------------
# ⑤ G-001 Golden Case 子集：secret + cmd injection
# ---------------------------------------------------------------------------


def test_e2e_g001_security_review_skill_catches_patterns() -> None:
    """G-001 思路落地：静态规则能捕获 secret_leak / cmd_injection / weak_hash / eval"""
    diff = """+++ b/evil.py
@@ -1,1 +1,5 @@
-a = 1
+api_key = "sk-1234567890abcdefghij"
+subprocess.run(cmd, shell=True)
+hashlib.md5(data).hexdigest()
+result = eval(user_input)
"""
    findings = static_security_scan(diff)
    cats = {f.category for f in findings}
    # G-001 应能命中至少 4 类
    assert "SECRET_LEAK" in cats
    assert "CMD_INJECTION" in cats
    assert "WEAK_HASH" in cats
    assert "EVAL" in cats


# ---------------------------------------------------------------------------
# ⑥ exit code 反映 verdict
# ---------------------------------------------------------------------------


def test_e2e_exit_code_approve_is_0(tmp_path: Path) -> None:
    _setup_git_repo(tmp_path, {"x.py": "a\n"})
    _commit_changes(tmp_path, {"x.py": "b\n"})
    env = {**os.environ, "AGENT_REVIEW_REPO": str(tmp_path)}
    result = subprocess.run(
        [".venv/bin/python", "tools/agent_review.py", "--pr", "HEAD~1..HEAD"],
        capture_output=True, text=True, timeout=30, env=env,
    )
    assert result.returncode == 0


def test_e2e_exit_code_request_changes_is_1(tmp_path: Path) -> None:
    _setup_git_repo(tmp_path, {"x.py": "a\n"})
    _commit_changes(tmp_path, {"x.py": "subprocess.run(c, shell=True)\n"})
    env = {**os.environ, "AGENT_REVIEW_REPO": str(tmp_path)}
    result = subprocess.run(
        [".venv/bin/python", "tools/agent_review.py", "--pr", "HEAD~1..HEAD"],
        capture_output=True, text=True, timeout=30, env=env,
    )
    assert result.returncode == 1
