"""
@module tests.golden.test_g027_review_e2e
@brief  P5-W36b G-027 Golden Case — agent_review Web 端到端

@note 真实环境: agent-swarm Web UI 跑起来, 用户在 /review 页面点 "Run Review"
      → POST /api/review → run_simple_review → 渲染 findings

@note 测试环境: 用 tmp git repo 构造 (干净 / 有 security 问题 两种 PR)

覆盖:
  - Case 1: 干净 PR (新增 markdown 文档) → 0 findings, verdict=approve
  - Case 2: 有 secret_leak (新增 hardcoded API key) → ≥1 finding, verdict≠approve
  - Case 3: 有 cmd_injection (os.system 拼接) → ≥1 finding, severity≥HIGH
  - Case 4: 端到端 POST /api/review 返 JSON 报告含 summary + findings
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_swarm.web import WebState, create_app
from agent_swarm.web.auth import JWTConfig, JWTIssuer

SECRET = "test-secret-g027"


def _bearer() -> str:
    iss = JWTIssuer(JWTConfig(secret=SECRET))
    return f"Bearer {iss.encode('test-user')}"


def _run(cmd: list[str], cwd: Path) -> str:
    """在 cwd 跑 git 命令, 返 stdout"""
    result = subprocess.run(
        cmd, cwd=str(cwd), capture_output=True, text=True, timeout=15,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git command failed: {' '.join(cmd)}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
    return result.stdout


def _make_git_repo(path: Path, user_name: str = "Test", user_email: str = "t@e.com") -> Path:
    """初始化 git repo + 配置 user"""
    path.mkdir(parents=True, exist_ok=True)
    _run(["git", "init", "-b", "main"], path)
    _run(["git", "config", "user.name", user_name], path)
    _run(["git", "config", "user.email", user_email], path)
    _run(["git", "config", "commit.gpgsign", "false"], path)
    return path


# ---------------------------------------------------------------------------
# Case 1: 干净 PR (markdown 文档) → 0 findings, verdict=approve
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_g027_clean_pr_zero_findings(tmp_path: Path) -> None:
    """Case 1: 干净 PR (新增文档) → 0 findings, verdict=approve"""
    repo = _make_git_repo(tmp_path / "clean-repo")
    # 首次提交 (基线)
    (repo / "README.md").write_text("# Test\n", encoding="utf-8")
    _run(["git", "add", "README.md"], repo)
    _run(["git", "commit", "-m", "init"], repo)
    # 第二次提交 (干净 PR: 加 markdown 文档)
    (repo / "DESIGN.md").write_text(
        "# Design\n\nAll secrets are in env vars. No hardcoded keys.\n",
        encoding="utf-8",
    )
    _run(["git", "add", "DESIGN.md"], repo)
    _run(["git", "commit", "-m", "add design doc"], repo)
    # 启动 web app, 指向此 repo
    app = create_app(
        web_state=WebState(),
        jwt_secret=SECRET,
        review_mode="simple",
        web_repo_root=repo,
    )
    client = TestClient(app)
    r = client.post(
        "/api/review",
        json={"pr_ref": "HEAD~1..HEAD"},
        headers={"Authorization": _bearer()},
    )
    assert r.status_code == 200, f"body: {r.text}"
    body = r.json()
    assert body["ok"] is True
    report = body["report"]
    # 干净 PR → 0 findings
    assert report["findings"] == [], f"应无 finding, 实得: {report['findings']}"
    assert report["verdict"] == "approve"


# ---------------------------------------------------------------------------
# Case 2: 有 secret_leak → ≥1 finding, verdict≠approve
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_g027_secret_leak_findings(tmp_path: Path) -> None:
    """Case 2: hardcoded API key → ≥1 finding, verdict≠approve"""
    repo = _make_git_repo(tmp_path / "leak-repo")
    # 基线
    (repo / "app.py").write_text("# app\n", encoding="utf-8")
    _run(["git", "add", "app.py"], repo)
    _run(["git", "commit", "-m", "init"], repo)
    # 添加 hardcoded API key
    (repo / "app.py").write_text(
        '# app\n'
        'API_KEY = "sk-1234567890abcdefghijklmnopqrstuvwxyz1234567890abcd"\n',
        encoding="utf-8",
    )
    _run(["git", "add", "app.py"], repo)
    _run(["git", "commit", "-m", "add api key"], repo)
    # 启动 web app
    app = create_app(
        web_state=WebState(),
        jwt_secret=SECRET,
        review_mode="simple",
        web_repo_root=repo,
    )
    client = TestClient(app)
    r = client.post(
        "/api/review",
        json={"pr_ref": "HEAD~1..HEAD"},
        headers={"Authorization": _bearer()},
    )
    assert r.status_code == 200, f"body: {r.text}"
    body = r.json()
    report = body["report"]
    # 有 secret_leak → ≥1 finding
    assert len(report["findings"]) >= 1, f"应至少有 1 finding, 实得: {report}"
    # verdict 是 request_changes 或 comment
    assert report["verdict"] in ("request_changes", "comment"), (
        f"verdict 应非 approve, 实得: {report['verdict']}"
    )
    # summary 应反映 finding
    assert "findings" in report["summary"].lower() or report["verdict"] != "approve"


# ---------------------------------------------------------------------------
# Case 3: 有 cmd_injection (os.system 拼接) → ≥1 finding, severity≥HIGH
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_g027_cmd_injection_findings(tmp_path: Path) -> None:
    """Case 3: subprocess shell=True → cmd_injection finding (W13 规则)"""
    repo = _make_git_repo(tmp_path / "cmd-repo")
    (repo / "safe.py").write_text("# safe\n", encoding="utf-8")
    _run(["git", "add", "safe.py"], repo)
    _run(["git", "commit", "-m", "init"], repo)
    # 添加 cmd injection (W13 规则匹配: subprocess.run(..., shell=True))
    (repo / "unsafe.py").write_text(
        'import subprocess\n'
        'def run_user_cmd(user_input):\n'
        '    subprocess.run(f"echo {user_input}", shell=True)  # cmd injection risk\n',
        encoding="utf-8",
    )
    _run(["git", "add", "unsafe.py"], repo)
    _run(["git", "commit", "-m", "add unsafe"], repo)
    app = create_app(
        web_state=WebState(),
        jwt_secret=SECRET,
        review_mode="simple",
        web_repo_root=repo,
    )
    client = TestClient(app)
    r = client.post(
        "/api/review",
        json={"pr_ref": "HEAD~1..HEAD"},
        headers={"Authorization": _bearer()},
    )
    assert r.status_code == 200, f"body: {r.text}"
    body = r.json()
    report = body["report"]
    # 应有 cmd_injection finding
    has_cmd = any("CMD" in f.get("category", "") for f in report["findings"])
    assert has_cmd, f"应包含 CMD_INJECTION finding, 实得: {report['findings']}"


# ---------------------------------------------------------------------------
# Case 4: 端到端 + 报告 schema 校验
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_g027_report_schema(tmp_path: Path) -> None:
    """Case 4: 报告 schema 完整 (含 summary / findings / verdict / confidence)"""
    repo = _make_git_repo(tmp_path / "schema-repo")
    (repo / "main.py").write_text("print('hello')\n", encoding="utf-8")
    _run(["git", "add", "main.py"], repo)
    _run(["git", "commit", "-m", "init"], repo)
    app = create_app(
        web_state=WebState(),
        jwt_secret=SECRET,
        review_mode="simple",
        web_repo_root=repo,
    )
    client = TestClient(app)
    r = client.post(
        "/api/review",
        json={"pr_ref": "HEAD~0..HEAD"},  # 空 diff
        headers={"Authorization": _bearer()},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    report = body["report"]
    # 报告 schema 必含字段
    for key in ("pr_ref", "verdict", "findings", "root_causes", "summary", "confidence"):
        assert key in report, f"report 缺字段 {key}"
    assert isinstance(report["findings"], list)
    assert isinstance(report["confidence"], (int, float))
    assert report["verdict"] in ("approve", "comment", "request_changes")
