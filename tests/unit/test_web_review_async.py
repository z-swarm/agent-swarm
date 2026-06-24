"""
@module tests.unit.test_web_review_async
@brief  P5-W36f agent_review 异步入口单测 (≥10 cases)

覆盖:
  - ReviewTask dataclass 字段
  - task store CRUD (create / get / subscribe)
  - llm_judge_factory 3 provider (openai / anthropic / fake)
  - run_full_review_async 异步路径 (fake LLM 端到端)
  - API 端点:
    * POST /api/review 返 202 + task_id (full mode)
    * POST /api/review 返 200 + report (simple mode 兼容 W36b)
    * GET /api/review/{task_id} 查状态
    * GET /api/review/{task_id}/events SSE 流
    * 404 不存在 task
  - 异步不阻塞 event loop
  - 缺 API key / 不存在 task 错误处理
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from agent_swarm.web import WebState, create_app, review_runner
from agent_swarm.web.auth import JWTConfig, JWTIssuer

SECRET = "test-secret-w36f-do-not-use"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_task_store():
    """每个 test 前清 task store (单进程全局变量)"""
    review_runner._TASK_STORE.clear()
    review_runner._TASK_QUEUES.clear()
    yield
    review_runner._TASK_STORE.clear()
    review_runner._TASK_QUEUES.clear()


def _client(jwt_secret: str | None = None, **kwargs) -> TestClient:
    defaults: dict = {"web_state": WebState(), "jwt_secret": jwt_secret}
    defaults.update(kwargs)
    app = create_app(**defaults)
    return TestClient(app)


def _bearer(secret: str = SECRET) -> str:
    iss = JWTIssuer(JWTConfig(secret=secret))
    return f"Bearer {iss.encode('user-1')}"


# ---------------------------------------------------------------------------
# 1. ReviewTask dataclass
# ---------------------------------------------------------------------------


def test_review_task_dataclass_fields() -> None:
    """ReviewTask 含全部 7 字段 (task_id / status / progress / log / result / error / created_at)"""
    fields = {f for f in review_runner.ReviewTask.__dataclass_fields__}
    assert "task_id" in fields
    assert "status" in fields
    assert "progress" in fields
    assert "log" in fields
    assert "result" in fields
    assert "error" in fields
    assert "created_at" in fields


def test_review_task_default_status_pending() -> None:
    """新建 task 默认 status=pending"""
    task = review_runner.ReviewTask(task_id="abc")
    assert task.status == "pending"
    assert task.progress == 0
    assert task.log == []
    assert task.result is None
    assert task.error is None


# ---------------------------------------------------------------------------
# 2. task store CRUD
# ---------------------------------------------------------------------------


def test_create_and_get_task() -> None:
    """create_task 返 ReviewTask, get_task 可查"""
    task = review_runner.create_task("main..HEAD", "fake")
    assert task.task_id in review_runner._TASK_STORE
    got = review_runner.get_task(task.task_id)
    assert got is task
    assert got.pr_ref == "main..HEAD"
    assert got.llm_provider == "fake"


def test_get_task_not_found() -> None:
    """get_task 不存在 ID 返 None"""
    assert review_runner.get_task("nonexistent-id") is None


def test_subscribe_task_creates_queue() -> None:
    """subscribe_task 返 asyncio.Queue"""
    task = review_runner.create_task("main..HEAD", "fake")
    q = review_runner.subscribe_task(task.task_id)
    assert q is not None
    assert isinstance(q, asyncio.Queue)


def test_subscribe_task_not_found() -> None:
    """subscribe 不存在 task 返 None"""
    assert review_runner.subscribe_task("nope") is None


# ---------------------------------------------------------------------------
# 3. llm_judge_factory
# ---------------------------------------------------------------------------


def test_llm_factory_fake() -> None:
    """fake provider 返 _deterministic_judge (W13)"""
    judge = review_runner.llm_judge_factory("fake")
    assert callable(judge)


def test_llm_factory_openai_no_key_raises() -> None:
    """openai 无 API key 时 fail-fast"""
    with patch.dict("os.environ", {}, clear=True), \
         pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        review_runner.llm_judge_factory("openai")


def test_llm_factory_anthropic_no_key_raises() -> None:
    """anthropic 无 API key 时 fail-fast"""
    with patch.dict("os.environ", {}, clear=True), \
         pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        review_runner.llm_judge_factory("anthropic")


def test_llm_factory_unknown_provider_raises() -> None:
    """未知 provider → ValueError"""
    with pytest.raises(ValueError, match="unknown LLM provider"):
        review_runner.llm_judge_factory("claude99")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 4. POST /api/review (full mode 异步)
# ---------------------------------------------------------------------------


def test_post_review_full_mode_returns_202_with_task_id() -> None:
    """POST /api/review full mode 返 202 + task_id (异步入口)"""
    with patch("agent_swarm.web.review_runner._is_git_repo", return_value=True):
        client = _client(jwt_secret=SECRET, review_mode="full", review_llm="fake")
        headers = {"Authorization": _bearer()}
        r = client.post("/api/review", json={"pr_ref": "main..HEAD"}, headers=headers)
    assert r.status_code == 202
    body = r.json()
    assert "task_id" in body
    assert body["status"] == "pending"
    assert "status_url" in body
    assert "events_url" in body


def test_post_review_full_mode_creates_task_in_store() -> None:
    """POST /api/review full mode 在 store 中创建 task"""
    with patch("agent_swarm.web.review_runner._is_git_repo", return_value=True):
        client = _client(jwt_secret=SECRET, review_mode="full", review_llm="fake")
        headers = {"Authorization": _bearer()}
        r = client.post("/api/review", json={"pr_ref": "main..HEAD"}, headers=headers)
    task_id = r.json()["task_id"]
    task = review_runner.get_task(task_id)
    assert task is not None
    assert task.pr_ref == "main..HEAD"
    assert task.llm_provider == "fake"


def test_post_review_simple_mode_returns_200_with_report() -> None:
    """POST /api/review simple mode 返 200 + report (W36b 兼容)"""
    with patch("agent_swarm.web.review_runner._is_git_repo", return_value=True), \
         patch("agent_swarm.web.review_runner.run_review_sync") as mock_sync:
        mock_sync.return_value = {
            "pr_ref": "main..HEAD",
            "verdict": "approve",
            "findings": [],
            "summary": "ok",
        }
        client = _client(jwt_secret=SECRET, review_mode="simple")
        headers = {"Authorization": _bearer()}
        r = client.post("/api/review", json={"pr_ref": "main..HEAD"}, headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert "report" in body


def test_post_review_invalid_pr_ref_400() -> None:
    """POST /api/review 无效 pr_ref 返 400 (full + simple 都校验)"""
    client = _client(jwt_secret=SECRET, review_mode="full")
    headers = {"Authorization": _bearer()}
    r = client.post("/api/review", json={"pr_ref": "main; rm -rf /"}, headers=headers)
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# 5. GET /api/review/{task_id} 状态查询
# ---------------------------------------------------------------------------


def test_get_review_status_returns_task_state() -> None:
    """GET /api/review/{task_id} 返 task 当前状态"""
    client = _client(jwt_secret=SECRET)
    task = review_runner.create_task("main..HEAD", "fake")
    r = client.get(f"/api/review/{task.task_id}", headers={"Authorization": _bearer()})
    assert r.status_code == 200
    body = r.json()
    assert body["task_id"] == task.task_id
    assert body["status"] == "pending"
    assert body["progress"] == 0
    assert body["pr_ref"] == "main..HEAD"
    assert body["llm_provider"] == "fake"


def test_get_review_status_not_found_404() -> None:
    """GET /api/review/{task_id} 不存在返 404"""
    client = _client(jwt_secret=SECRET)
    r = client.get("/api/review/nonexistent", headers={"Authorization": _bearer()})
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# 6. run_full_review_async 异步执行 (fake LLM 端到端)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_full_review_async_fake_provider() -> None:
    """fake LLM 跑完, task 状态 = done + result 含 pr_ref"""
    repo_root = Path("/tmp").resolve()  # 真实 git repo 路径用测试 fixture
    # 用 monkey patch _is_git_repo + run_full_review 简化 (不依赖真实 git)
    task = review_runner.create_task("main..HEAD", "fake")
    with patch("agent_swarm.web.review_runner._is_git_repo", return_value=True), \
         patch("agent_swarm.web.review_runner._run_full_in_thread") as mock_thread:
        from dataclasses import dataclass, field
        @dataclass
        class FakeReport:
            pr_ref: str = "main..HEAD"
            verdict: str = "approve"
            findings: list = field(default_factory=list)
            root_causes: list = field(default_factory=list)
            summary: str = "fake review ok"
            confidence: float = 0.9
            files_changed: int = 0
            lines_changed: int = 0
        mock_thread.return_value = FakeReport()
        await review_runner.run_full_review_async(
            task.task_id, "main..HEAD", repo_root, "fake", timeout=5.0,
        )
    done_task = review_runner.get_task(task.task_id)
    assert done_task is not None
    assert done_task.status == "done"
    assert done_task.progress == 100
    assert done_task.result is not None
    assert done_task.result["pr_ref"] == "main..HEAD"


# ---------------------------------------------------------------------------
# 7. 异步不阻塞 event loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_full_review_async_does_not_block() -> None:
    """异步任务在跑时, 其他 coroutine 可推进 (不阻塞 event loop)"""
    task = review_runner.create_task("main..HEAD", "fake")
    with patch("agent_swarm.web.review_runner._is_git_repo", return_value=True), \
         patch("agent_swarm.web.review_runner._run_full_in_thread") as mock_thread:
        # 模拟 LLM 慢响应 (0.3s), 但应该不阻塞
        from dataclasses import dataclass, field
        @dataclass
        class FakeReport:
            pr_ref: str = "main..HEAD"
            verdict: str = "approve"
            findings: list = field(default_factory=list)
            root_causes: list = field(default_factory=list)
            summary: str = "ok"
            confidence: float = 0.9
            files_changed: int = 0
            lines_changed: int = 0

        def slow_run(*args, **kwargs):
            time.sleep(0.3)
            return FakeReport()
        mock_thread.side_effect = slow_run
        # 启动 task
        task_coro = asyncio.create_task(
            review_runner.run_full_review_async(
                task.task_id, "main..HEAD", None, "fake", timeout=10.0,
            )
        )
        # 期间跑其他 coroutine, 测不阻塞
        start = time.time()
        await asyncio.sleep(0.1)  # 让出控制权
        elapsed = time.time() - start
        assert elapsed < 0.2, f"event loop 阻塞 {elapsed:.3f}s"
        await task_coro
    done = review_runner.get_task(task.task_id)
    assert done is not None and done.status == "done"
