"""
@module tests.golden.test_g029_review_async_e2e
@brief  P5-W36f G-029 Golden Case — agent_review 异步入口 (full mode + SSE) 端到端

@note W36b G-027 走 simple mode 同步; G-029 走 full mode 异步 + LLM judge
@note 真实流程: POST /api/review → 202 + task_id → 后台 fake LLM 跑 review
                → GET /api/review/{task_id}/events SSE 推进度 → done
                → GET /api/review/{task_id} 查结果

覆盖:
  - Case 1: 干净 PR (markdown 文档) → 0 findings, verdict=approve, async 完成
  - Case 2: SSE 流事件序列 ≥1 条 (snapshot / progress / done)
  - Case 3: 异步任务从 pending → done, progress 0→100
  - Case 4: 报告 schema 完整 (含 summary / findings / verdict / confidence)
  - Case 5: 404 不存在 task_id
"""

from __future__ import annotations

import contextlib
import json
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_swarm.web import WebState, create_app, review_runner
from agent_swarm.web.auth import JWTConfig, JWTIssuer

SECRET = "test-secret-g029"


@pytest.fixture(autouse=True)
def _clean_task_store():
    """每个 test 前清 task store (单进程全局变量)"""
    review_runner._TASK_STORE.clear()
    review_runner._TASK_QUEUES.clear()
    yield
    review_runner._TASK_STORE.clear()
    review_runner._TASK_QUEUES.clear()


def _bearer() -> str:
    iss = JWTIssuer(JWTConfig(secret=SECRET))
    return f"Bearer {iss.encode('test-user')}"


def _run(cmd: list[str], cwd: Path) -> str:
    result = subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git command failed: {' '.join(cmd)}\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
    return result.stdout


def _make_git_repo(path: Path, user_name: str = "Test", user_email: str = "t@e.com") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _run(["git", "init", "-b", "main"], path)
    _run(["git", "config", "user.name", user_name], path)
    _run(["git", "config", "user.email", user_email], path)
    _run(["git", "config", "commit.gpgsign", "false"], path)
    return path


def _make_client(repo_root: Path) -> TestClient:
    """W36f 模式: full mode + fake LLM"""
    app = create_app(
        web_state=WebState(),
        jwt_secret=SECRET,
        web_repo_root=repo_root,
        review_mode="full",
        review_llm="fake",
        review_timeout=10.0,
    )
    return TestClient(app)


def _wait_done(client: TestClient, task_id: str, timeout_s: float = 5.0) -> dict:
    """轮询 task 直到 done/error 或超时"""
    deadline = time.time() + timeout_s
    last: dict = {}
    while time.time() < deadline:
        r = client.get(f"/api/review/{task_id}", headers={"Authorization": _bearer()})
        last = r.json()
        if last.get("status") in ("done", "error"):
            return last
        time.sleep(0.05)
    return last


# ---------------------------------------------------------------------------
# Case 1: 干净 PR 异步跑完
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_g029_clean_pr_async_done(tmp_path: Path) -> None:
    """Case 1: 干净 PR → 异步任务完成, verdict=approve"""
    repo = _make_git_repo(tmp_path / "clean-repo")
    (repo / "README.md").write_text("# Test\n", encoding="utf-8")
    _run(["git", "add", "README.md"], repo)
    _run(["git", "commit", "-m", "init"], repo)
    (repo / "DESIGN.md").write_text(
        "# Design\n\nAll secrets are in env vars.\n",
        encoding="utf-8",
    )
    _run(["git", "add", "DESIGN.md"], repo)
    _run(["git", "commit", "-m", "add design doc"], repo)
    client = _make_client(repo)
    r = client.post(
        "/api/review",
        json={"pr_ref": "HEAD~1..HEAD"},
        headers={"Authorization": _bearer()},
    )
    assert r.status_code == 202, f"应返 202, 实得 {r.status_code}: {r.text}"
    task_id = r.json()["task_id"]
    final = _wait_done(client, task_id)
    assert final["status"] == "done", f"应 done, 实得 {final}"
    assert "result" in final
    assert final["result"]["verdict"] == "approve"
    assert final["result"]["findings"] == []


# ---------------------------------------------------------------------------
# Case 2: SSE 流事件序列
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_g029_sse_event_stream(tmp_path: Path) -> None:
    """Case 2: SSE 流发 ≥1 条事件 (含 status)"""
    repo = _make_git_repo(tmp_path / "sse-repo")
    (repo / "x.md").write_text("# x\n", encoding="utf-8")
    _run(["git", "add", "x.md"], repo)
    _run(["git", "commit", "-m", "init"], repo)
    (repo / "y.md").write_text("# y\n", encoding="utf-8")
    _run(["git", "add", "y.md"], repo)
    _run(["git", "commit", "-m", "add y"], repo)
    client = _make_client(repo)
    r = client.post(
        "/api/review",
        json={"pr_ref": "HEAD~1..HEAD"},
        headers={"Authorization": _bearer()},
    )
    task_id = r.json()["task_id"]
    with client.stream(
        "GET",
        f"/api/review/{task_id}/events",
        headers={"Authorization": _bearer()},
    ) as response:
        events: list[dict] = []
        for line in response.iter_lines():
            if line.startswith("data: "):
                with contextlib.suppress(json.JSONDecodeError):
                    events.append(json.loads(line[6:]))
            if len(events) >= 3:
                break
    assert len(events) >= 1, f"SSE 应至少 1 条事件, 实得 {events}"
    statuses = {e.get("status") for e in events}
    assert "done" in statuses or "running" in statuses, (
        f"事件应含 done 或 running, 实得 statuses={statuses}"
    )


# ---------------------------------------------------------------------------
# Case 3: 异步任务状态变化
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_g029_task_state_progression(tmp_path: Path) -> None:
    """Case 3: task 状态从 pending → done, progress 0→100, log ≥1 条"""
    repo = _make_git_repo(tmp_path / "prog-repo")
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    _run(["git", "add", "a.txt"], repo)
    _run(["git", "commit", "-m", "init"], repo)
    (repo / "b.txt").write_text("b\n", encoding="utf-8")
    _run(["git", "add", "b.txt"], repo)
    _run(["git", "commit", "-m", "add b"], repo)
    client = _make_client(repo)
    r = client.post(
        "/api/review",
        json={"pr_ref": "HEAD~1..HEAD"},
        headers={"Authorization": _bearer()},
    )
    task_id = r.json()["task_id"]
    final = _wait_done(client, task_id)
    assert final["status"] == "done", f"应 done, 实得 {final}"
    assert final["progress"] == 100
    assert "log" in final
    assert len(final["log"]) >= 1


# ---------------------------------------------------------------------------
# Case 4: 报告 schema 完整
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_g029_report_schema(tmp_path: Path) -> None:
    """Case 4: 报告 schema 完整 (含 pr_ref / verdict / findings / root_causes / summary / confidence)"""
    repo = _make_git_repo(tmp_path / "schema-repo")
    (repo / "main.py").write_text("print('hi')\n", encoding="utf-8")
    _run(["git", "add", "main.py"], repo)
    _run(["git", "commit", "-m", "init"], repo)
    client = _make_client(repo)
    r = client.post(
        "/api/review",
        json={"pr_ref": "HEAD~0..HEAD"},
        headers={"Authorization": _bearer()},
    )
    task_id = r.json()["task_id"]
    final = _wait_done(client, task_id)
    assert final["status"] == "done"
    report = final["result"]
    for key in ("pr_ref", "verdict", "findings", "root_causes", "summary", "confidence"):
        assert key in report, f"report 缺 {key}"
    assert isinstance(report["findings"], list)
    assert isinstance(report["confidence"], (int, float))


# ---------------------------------------------------------------------------
# Case 5: 404 不存在 task
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_g029_task_not_found_404(tmp_path: Path) -> None:
    """Case 5: GET 不存在 task_id (status + events) 都返 404"""
    repo = _make_git_repo(tmp_path / "404-repo")
    (repo / "x.txt").write_text("x\n", encoding="utf-8")
    _run(["git", "add", "x.txt"], repo)
    _run(["git", "commit", "-m", "init"], repo)
    client = _make_client(repo)
    r = client.get(
        "/api/review/nonexistent-task-id",
        headers={"Authorization": _bearer()},
    )
    assert r.status_code == 404
    r2 = client.get(
        "/api/review/nonexistent-task-id/events",
        headers={"Authorization": _bearer()},
    )
    assert r2.status_code == 404
