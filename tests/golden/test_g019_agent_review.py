"""
@module tests.golden.test_g019_agent_review
@brief  W15-⑦ G-019 Golden Case: agent_review.py 识别真问题

测试策略:
  直接 import tools.agent_review 的 static_security_scan + run_simple_review
  构造 fixture diff 跑 —— 不通过 subprocess（agent_review 的 REPO 硬编码）
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from agent_swarm.golden import load_expectation

# 直接 import tools.agent_review（不通过 cli）
TOOLS_DIR = Path(__file__).resolve().parent.parent.parent / "tools"
sys.path.insert(0, str(TOOLS_DIR))
_spec = importlib.util.spec_from_file_location(
    "agent_review",
    TOOLS_DIR / "agent_review.py",
)
assert _spec and _spec.loader
agent_review = importlib.util.module_from_spec(_spec)
sys.modules["agent_review"] = agent_review
_spec.loader.exec_module(agent_review)

CASES_ROOT = Path(__file__).parent / "cases"


# ---------------------------------------------------------------------------
# Fixture: 含 4 类已知安全问题的 diff
# ---------------------------------------------------------------------------


VULNERABLE_DIFF = """diff --git a/app.py b/app.py
index 1234567..abcdef0 100644
--- a/app.py
+++ b/app.py
@@ -1,3 +1,35 @@
+import os
+import sqlite3
+import subprocess
+import hashlib
+
+# 1) 硬编码凭证（secret_leak）→ SECRET_LEAK
+AWS_SECRET_KEY = "AKIA1234567890ABCDEF"
+GITHUB_TOKEN = "ghp_1234567890abcdefghijklmnopqrstuvwxyz"
+
 def get_user(name):
-    return None
+    # 2) SQL 注入 → SQL_INJECTION（f-string 拼接 SELECT）
+    conn = sqlite3.connect("app.db")
+    return conn.execute(f"SELECT * FROM users WHERE name = '{name}'").fetchall()
+
 def list_files(user_input):
-    return []
+    # 3) Path traversal → PATH_TRAVERSAL（open() 拼接 user_input）
+    with open("/var/data/" + user_input) as f:
+        return f.read()
+
 def ping(host):
-    return b""
+    # 4) Command injection → CMD_INJECTION（subprocess.run + shell=True）
+    return subprocess.run(f"ping -c 1 {host}", shell=True, capture_output=True)
+
 def hash_pw(pw):
+    # 5) Weak hash → WEAK_HASH
+    return hashlib.md5(pw.encode()).hexdigest()
"""


CLEAN_DIFF = """diff --git a/app.py b/app.py
index 1234567..abcdef0 100644
--- a/app.py
+++ b/app.py
@@ -1,3 +1,6 @@
 def add(a, b):
     return a + b
+
+def sub(a, b):
+    return a - b
"""


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_g019_agent_review_finds_real_issues() -> None:
    """G-019 主 case: static_security_scan 识别真问题"""
    findings = agent_review.static_security_scan(VULNERABLE_DIFF)
    categories = {f.category for f in findings}
    print(f"  found categories: {sorted(categories)}")
    # W13 实际 category 是 UPPER_CASE（与 _RULES 定义一致）
    assert "SECRET_LEAK" in categories, f"missing SECRET_LEAK: {categories}"
    assert "SQL_INJECTION" in categories, f"missing SQL_INJECTION: {categories}"
    assert "PATH_TRAVERSAL" in categories, f"missing PATH_TRAVERSAL: {categories}"
    assert "CMD_INJECTION" in categories, f"missing CMD_INJECTION: {categories}"
    assert "WEAK_HASH" in categories, f"missing WEAK_HASH: {categories}"


def test_g019_no_false_positive_on_clean_diff() -> None:
    """G-019 副 case: 干净 diff → 无 CRITICAL/HIGH finding"""
    findings = agent_review.static_security_scan(CLEAN_DIFF)
    critical_high = [f for f in findings if f.severity in ("CRITICAL", "HIGH")]
    assert len(critical_high) == 0, f"clean diff triggered false positive: {critical_high}"


def test_g019_run_simple_review_verdict() -> None:
    """run_simple_review: vulnerable code → request_changes verdict"""
    import unittest.mock as mock

    with mock.patch.object(
        agent_review,
        "get_pr_diff",
        return_value=(VULNERABLE_DIFF, 1, 35),
    ):
        report = agent_review.run_simple_review("main..HEAD")

    assert report.verdict == "request_changes", (
        f"expected request_changes, got {report.verdict}: {report.summary}"
    )
    assert len(report.findings) >= 4
    assert report.files_changed == 1


def test_g019_run_simple_review_clean_approves() -> None:
    """run_simple_review: clean diff → approve"""
    import unittest.mock as mock

    with mock.patch.object(
        agent_review,
        "get_pr_diff",
        return_value=(CLEAN_DIFF, 1, 3),
    ):
        report = agent_review.run_simple_review("main..HEAD")

    # clean diff 不应触发 request_changes
    assert report.verdict == "approve", f"expected approve, got {report.verdict}"


def test_g019_expected_yaml_loads() -> None:
    """expected.yaml schema 合法"""
    case_dir = CASES_ROOT / "G-019_agent_review_security_audit"
    exp = load_expectation(case_dir)
    assert exp.case_id == "G-019"
    assert exp.phase == 2
    assert exp.title
    kws = [m.get("keyword", "").lower() for m in exp.must_find]
    assert "secret" in kws
    assert "injection" in kws
    assert "traversal" in kws


def test_g019_finding_severity_levels() -> None:
    """findings 应有合理严重度分级"""
    findings = agent_review.static_security_scan(VULNERABLE_DIFF)
    secret_findings = [f for f in findings if f.category == "SECRET_LEAK"]
    assert secret_findings, "no SECRET_LEAK finding"
    assert any(f.severity in ("CRITICAL", "HIGH") for f in secret_findings), (
        f"SECRET_LEAK not marked high enough: {[f.severity for f in secret_findings]}"
    )


def test_g019_review_finding_has_required_fields() -> None:
    """ReviewFinding dataclass 必填字段都有"""
    findings = agent_review.static_security_scan(VULNERABLE_DIFF)
    assert findings, "no findings to inspect"
    for f in findings[:3]:
        assert f.severity in ("CRITICAL", "HIGH", "MEDIUM", "LOW")
        assert f.file
        assert f.line >= 0
        assert f.category
        assert f.description
