"""单元测试：tools/agent_review.py——W13 Dogfooding PR 审查工具"""

from __future__ import annotations

import json
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


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


def test_review_finding_dataclass() -> None:
    f = ReviewFinding(
        severity="HIGH", file="x.py", line=10,
        category="XSS", description="bad"
    )
    assert f.severity == "HIGH"
    d = f.__dict__
    assert d["file"] == "x.py"


def test_review_report_dataclass_defaults() -> None:
    r = ReviewReport(pr_ref="main..HEAD", verdict="approve")
    assert r.findings == []
    assert r.root_causes == []
    assert r.summary == ""
    assert r.confidence == 0.0


# ---------------------------------------------------------------------------
# get_pr_diff
# ---------------------------------------------------------------------------


def test_get_pr_diff_returns_stats(tmp_path) -> None:
    """在临时 git 仓库里跑 get_pr_diff"""
    # Init repo
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=tmp_path, check=True
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=tmp_path, check=True
    )
    (tmp_path / "x.py").write_text("a = 1\n")
    subprocess.run(["git", "add", "x.py"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "init"],
        cwd=tmp_path, check=True
    )
    (tmp_path / "x.py").write_text("a = 1\nb = 2\nc = 3\n")
    subprocess.run(["git", "add", "x.py"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "add lines"],
        cwd=tmp_path, check=True
    )
    # 用 monkey-patch REPO
    import tools.agent_review
    orig_repo = tools.agent_review.REPO
    tools.agent_review.REPO = tmp_path
    try:
        diff, files, lines = get_pr_diff("HEAD~1..HEAD")
        assert files == 1
        assert lines == 2  # b=2 / c=3
        assert "+b = 2" in diff
    finally:
        tools.agent_review.REPO = orig_repo


# ---------------------------------------------------------------------------
# static_security_scan
# ---------------------------------------------------------------------------


def test_static_scan_detects_subprocess_shell_true() -> None:
    """subprocess shell=True → HIGH CMD_INJECTION"""
    diff = """+++ b/evil.py
@@ -1,1 +1,1 @@
-old
+subprocess.run(cmd, shell=True)
"""
    findings = static_security_scan(diff)
    assert any(f.category == "CMD_INJECTION" for f in findings)


def test_static_scan_detects_eval() -> None:
    """eval() → HIGH"""
    diff = """+++ b/evil.py
@@ -1,1 +1,1 @@
-old
+result = eval(user_input)
"""
    findings = static_security_scan(diff)
    assert any(f.category == "EVAL" for f in findings)


def test_static_scan_detects_sql_injection() -> None:
    """SQL 字符串拼接 → HIGH SQL_INJECTION"""
    diff = """+++ b/evil.py
@@ -1,1 +1,1 @@
-old
+cursor.execute(f"SELECT * FROM users WHERE id = {uid}")
"""
    findings = static_security_scan(diff)
    assert any(f.category == "SQL_INJECTION" for f in findings)


def test_static_scan_detects_weak_hash() -> None:
    """MD5 → MEDIUM WEAK_HASH"""
    diff = """+++ b/evil.py
@@ -1,1 +1,1 @@
-old
+hashlib.md5(data).hexdigest()
"""
    findings = static_security_scan(diff)
    assert any(f.category == "WEAK_HASH" for f in findings)


def test_static_scan_skips_secret_manager_references() -> None:
    """${VAR} 引用 → 不应被当作硬编码密钥"""
    diff = """+++ b/cfg.py
@@ -1,1 +1,1 @@
-old
+app_secret = "${LARK_APP_SECRET}"
"""
    findings = static_security_scan(diff)
    # SecretManager 引用应被跳过
    assert not any(f.category == "SECRET_LEAK" for f in findings)


def test_static_scan_detects_real_secret_leak() -> None:
    """明文 api_key = "sk-..." → 应被检测"""
    diff = """+++ b/cfg.py
@@ -1,1 +1,1 @@
-old
+api_key = "sk-1234567890abcdefg"
"""
    findings = static_security_scan(diff)
    assert any(f.category == "SECRET_LEAK" for f in findings)


def test_static_scan_clean_diff_no_findings() -> None:
    """干净 diff → 无 finding"""
    diff = """+++ b/x.py
@@ -1,1 +1,1 @@
-old = 1
+new = 2
"""
    findings = static_security_scan(diff)
    assert findings == []


def test_static_scan_ignores_removed_lines() -> None:
    """只在 + 添加行检查（不查 - 删除行）"""
    diff = """+++ b/x.py
@@ -1,2 +1,1 @@
-old
-subprocess.run(cmd, shell=True)
+safe_call()
"""
    findings = static_security_scan(diff)
    # 删除行不应被检测
    assert not any(f.category == "CMD_INJECTION" for f in findings)


# ---------------------------------------------------------------------------
# P1-NEW-1 修复：EVAL 规则收紧 + 源码白名单
# ---------------------------------------------------------------------------


def test_static_scan_skips_non_source_files() -> None:
    """MD / JSON / YAML / TXT 等非源码文件不应被扫到"""
    diff = """+++ b/README.md
@@ -1,1 +1,1 @@
-old
+可以使用 `eval()` 转换字符串,但生产代码不要用
"""
    findings = static_security_scan(diff)
    assert not any(f.category == "EVAL" for f in findings)


def test_static_scan_skips_venv_and_node_modules() -> None:
    """`.venv/` / `node_modules/` 路径应被跳过"""
    diff = """+++ b/.venv/lib/foo.py
@@ -1,1 +1,1 @@
-old
+result = eval(user_input)
"""
    findings = static_security_scan(diff)
    # .venv 是第三方库,不应被扫到
    assert not any(f.category == "EVAL" for f in findings)


def test_static_scan_eval_skips_method_call() -> None:
    """`self.eval(...)` 是方法调用,不是 builtin eval,不应命中"""
    diff = """+++ b/x.py
@@ -1,1 +1,1 @@
-old
+self.eval(user_input)
"""
    findings = static_security_scan(diff)
    # self.eval() 是方法调用,不是内置 eval
    assert not any(f.category == "EVAL" for f in findings)


def test_static_scan_eval_skips_string_literal() -> None:
    """字符串字面量里含 'eval(' 不应被命中"""
    diff = """+++ b/x.py
@@ -1,1 +1,1 @@
-old
+msg = "never use eval("
"""
    findings = static_security_scan(diff)
    # 字符串字面量里的 "eval(" 不应命中
    assert not any(f.category == "EVAL" for f in findings)


def test_static_scan_eval_catches_real_builtin_eval() -> None:
    """真实内置 eval(user_input) 应被检测"""
    diff = """+++ b/x.py
@@ -1,1 +1,1 @@
-old
+result = eval(user_input)
"""
    findings = static_security_scan(diff)
    assert any(f.category == "EVAL" for f in findings)


def test_static_scan_eval_catches_exec_call() -> None:
    """exec(open('x').read()) 应被检测"""
    diff = """+++ b/x.py
@@ -1,1 +1,1 @@
-old
+exec(open('x').read())
"""
    findings = static_security_scan(diff)
    assert any(f.category == "EVAL" for f in findings)


def test_static_scan_eval_skips_word_substrings() -> None:
    """'evaluate' / 'developer' / 'execution' 不应命中"""
    diff = """+++ b/x.py
@@ -1,1 +1,1 @@
-old
+x = developer_name + " evaluate: " + result
"""
    findings = static_security_scan(diff)
    # 词边界排除 evaluate/developer
    assert not any(f.category == "EVAL" for f in findings)


def test_static_scan_yaml_subprocess_string_not_flagged() -> None:
    """YAML 里的 'subprocess.run' 字符串不应触发 CMD_INJECTION"""
    diff = """+++ b/config.yaml
@@ -1,1 +1,1 @@
-old
+doc: "use subprocess.run with shell=True carefully"
"""
    findings = static_security_scan(diff)
    # .yaml 不是源码,不应被扫
    assert not any(f.category == "CMD_INJECTION" for f in findings)


def test_static_scan_json_file_skipped() -> None:
    """.json 文件不扫"""
    diff = """+++ b/manifest.json
@@ -1,1 +1,1 @@
-old
+{"api_key": "sk-1234567890abcdefghij"}
"""
    findings = static_security_scan(diff)
    # .json 不是源码,SecretManager 引用检测也不该在此触发
    assert not any(f.category == "SECRET_LEAK" for f in findings)


# ---------------------------------------------------------------------------
# run_simple_review
# ---------------------------------------------------------------------------


def test_run_simple_review_clean_diff_returns_approve(tmp_path) -> None:
    """干净 diff → verdict=approve"""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True
    )
    subprocess.run(
        ["git", "config", "user.name", "T"], cwd=tmp_path, check=True
    )
    (tmp_path / "x.py").write_text("a = 1\n")
    subprocess.run(["git", "add", "x.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "i"], cwd=tmp_path, check=True)
    (tmp_path / "x.py").write_text("b = 2\n")
    subprocess.run(["git", "add", "x.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "u"], cwd=tmp_path, check=True)
    import tools.agent_review
    orig = tools.agent_review.REPO
    tools.agent_review.REPO = tmp_path
    try:
        report = run_simple_review("HEAD~1..HEAD")
    finally:
        tools.agent_review.REPO = orig
    assert report.verdict == "approve"
    assert "无" in report.summary or "approve" in report.summary


def test_run_simple_review_dirty_diff_returns_request_changes(tmp_path) -> None:
    """有 critical finding → verdict=request_changes"""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True
    )
    subprocess.run(
        ["git", "config", "user.name", "T"], cwd=tmp_path, check=True
    )
    (tmp_path / "x.py").write_text("# init\n")
    subprocess.run(["git", "add", "x.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "i"], cwd=tmp_path, check=True)
    (tmp_path / "x.py").write_text('api_key = "sk-1234567890abcdefghijklmnop"\n')
    subprocess.run(["git", "add", "x.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "u"], cwd=tmp_path, check=True)
    import tools.agent_review
    orig = tools.agent_review.REPO
    tools.agent_review.REPO = tmp_path
    try:
        report = run_simple_review("HEAD~1..HEAD")
    finally:
        tools.agent_review.REPO = orig
    assert report.verdict == "request_changes"
    assert any(f.category == "SECRET_LEAK" for f in report.findings)


# ---------------------------------------------------------------------------
# CLI 集成
# ---------------------------------------------------------------------------


def test_cli_runs_and_outputs_json(tmp_path) -> None:
    """CLI --output=json 模式"""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True
    )
    subprocess.run(
        ["git", "config", "user.name", "T"], cwd=tmp_path, check=True
    )
    (tmp_path / "x.py").write_text("a\n")
    subprocess.run(["git", "add", "x.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "i"], cwd=tmp_path, check=True)
    (tmp_path / "x.py").write_text("b\n")
    subprocess.run(["git", "add", "x.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "u"], cwd=tmp_path, check=True)

    # 通过 env var AGENT_REVIEW_REPO 覆盖默认 REPO
    env = {"AGENT_REVIEW_REPO": str(tmp_path), "PATH": "/usr/bin:/bin"}
    # tools/agent_review.py 在 git 仓库根目录；cwd 用项目根
    import os
    result = subprocess.run(
        [".venv/bin/python", "tools/agent_review.py",
         "--pr", "HEAD~1..HEAD", "--output", "json"],
        capture_output=True, text=True, timeout=30,
        env={**os.environ, **env},
    )
    assert result.returncode == 0, f"stderr={result.stderr}"
    data = json.loads(result.stdout)
    assert data["verdict"] == "approve"
    assert data["pr_ref"] == "HEAD~1..HEAD"
    assert data["files_changed"] == 1
