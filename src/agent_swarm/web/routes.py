"""
@module agent_swarm.web.routes
@brief  P5-W28 HTML 路由 (HTMX)

4 个页面 + 5 个 partial:
  - GET /             Dashboard (metrics + events)
  - GET /agents       Agent 列表
  - GET /worktrees    Worktree 状态
  - GET /tasks        任务队列
  - GET /partials/events       最近事件 (HTMX 刷新)
  - GET /partials/metrics      实时 metrics
  - GET /partials/agents       Agent 列表 fragment
  - GET /partials/worktrees    Worktree 列表 fragment
  - GET /partials/tasks        Task 列表 fragment
P5-W36b: + /review 页面 + POST /api/review (调 run_simple_review)
"""


from __future__ import annotations

import asyncio
import logging
import shlex
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from agent_swarm.web.state import WebState

log = logging.getLogger(__name__)

router = APIRouter()

# W36b: pr_ref 注入防御 — 禁 shell 危险字符
_UNSAFE_PR_CHARS = (";", "&", "|", "`", "$", ">", "<", "\n", "\r")


def _templates(request: Request):
    """取 app 上的 Jinja2 templates 实例"""
    return request.app.state.templates


def _state(request: Request) -> WebState:
    return request.app.state.web_state


def _validate_pr_ref(pr_ref: str) -> str | None:
    """
    @brief 校验 pr_ref 防止 shell 注入

    @param pr_ref  形如 "main..HEAD" / "abc123..def456" / "main..HEAD -- path"
    @return 错误信息 (None = 通过)
    """
    if not pr_ref:
        return "pr_ref cannot be empty"
    if any(c in pr_ref for c in _UNSAFE_PR_CHARS):
        return f"pr_ref contains unsafe characters: {pr_ref!r}"
    # 用 shlex 校验可解析
    try:
        shlex.split(pr_ref)
    except ValueError as exc:
        return f"pr_ref cannot be parsed: {exc}"
    return None


# ---------------------------------------------------------------------------
# 页面
# ---------------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> Response:
    """Dashboard: 概览 + 实时事件流"""
    state = _state(request)
    tpl = _templates(request)
    return tpl.TemplateResponse(
        request,
        "dashboard.html",
        {
            "page": "dashboard",
            "state": state,
        },
    )


@router.get("/agents", response_class=HTMLResponse)
async def agents_page(request: Request) -> Response:
    tpl = _templates(request)
    return tpl.TemplateResponse(
        request, "agents.html", {"page": "agents"},
    )


@router.get("/worktrees", response_class=HTMLResponse)
async def worktrees_page(request: Request) -> Response:
    tpl = _templates(request)
    return tpl.TemplateResponse(
        request, "worktrees.html", {"page": "worktrees"},
    )


@router.get("/tasks", response_class=HTMLResponse)
async def tasks_page(request: Request) -> Response:
    tpl = _templates(request)
    return tpl.TemplateResponse(
        request, "tasks.html", {"page": "tasks"},
    )


# ---------------------------------------------------------------------------
# HTMX partials (返回 HTML 片段, 不带 layout)
# ---------------------------------------------------------------------------


@router.get("/partials/events", response_class=HTMLResponse)
async def partial_events(request: Request) -> Response:
    """最近 N 条事件 — HTMX 每 2s 刷新"""
    state = _state(request)
    tpl = _templates(request)
    return tpl.TemplateResponse(
        request,
        "partials/events.html",
        {"events": state.recent_events(50)},
    )


@router.get("/partials/metrics", response_class=HTMLResponse)
async def partial_metrics(request: Request) -> Response:
    """实时 metrics (session count / event by type) — HTMX 每 5s 刷新"""
    state = _state(request)
    tpl = _templates(request)
    return tpl.TemplateResponse(
        request,
        "partials/metrics.html",
        {
            "session_count": state.session_count(),
            "uptime_seconds": int(state.uptime_seconds()),
            "events_by_type": state.events_by_type(),
            "total_events": len(state.events),
        },
    )


@router.get("/partials/agents", response_class=HTMLResponse)
async def partial_agents(request: Request) -> Response:
    """Agent 列表 fragment"""
    state = _state(request)
    # 简化: session 视为 agent
    agents = [
        {"id": sid, **data}
        for sid, data in state.active_sessions.items()
    ]
    tpl = _templates(request)
    return tpl.TemplateResponse(
        request, "partials/agents.html", {"agents": agents},
    )


@router.get("/partials/worktrees", response_class=HTMLResponse)
async def partial_worktrees(request: Request) -> Response:
    """Worktree 状态 fragment (P4-W22 集成)"""
    tpl = _templates(request)
    worktrees: list[dict] = []
    # 若 state 含 wm (WorktreeManager), 拉活跃
    wm = getattr(request.app.state, "worktree_manager", None)
    if wm is not None:
        for h in wm.list_active():
            worktrees.append({
                "key": h.key,
                "path": str(h.path),
                "branch": h.branch,
                "agent_id": h.agent_id,
                "tenant_id": h.tenant_id,
                "session_id": h.session_id,
            })
    return tpl.TemplateResponse(
        request, "partials/worktrees.html", {"worktrees": worktrees},
    )


@router.get("/partials/tasks", response_class=HTMLResponse)
async def partial_tasks(request: Request) -> Response:
    """Task 列表 fragment"""
    state = _state(request)
    # 从 events 推断 task 状态 (简化: 显示最近 task 类事件)
    tasks: list[dict] = []
    for rec in state.recent_events(20):
        if "task" in rec.event_name.lower():
            tasks.append({
                "event": rec.event_name,
                "session_id": rec.session_id[:12],
                "ts": rec.timestamp,
                "payload": rec.payload,
            })
    tpl = _templates(request)
    return tpl.TemplateResponse(
        request, "partials/tasks.html", {"tasks": tasks},
    )


# ---------------------------------------------------------------------------
# JSON API (HTMX fallback / SPA 集成用)
# ---------------------------------------------------------------------------


@router.get("/api/state")
async def api_state(request: Request) -> JSONResponse:
    """完整状态 JSON (调试 + 第三方集成)"""
    state = _state(request)
    return JSONResponse({
        "uptime_seconds": state.uptime_seconds(),
        "session_count": state.session_count(),
        "total_events": len(state.events),
        "events_by_type": state.events_by_type(),
        "active_sessions": state.active_sessions,
    })


@router.get("/api/events")
async def api_events(request: Request, limit: int = 50) -> JSONResponse:
    """最近事件 JSON"""
    state = _state(request)
    return JSONResponse({
        "events": [
            {
                "event_name": e.event_name,
                "session_id": e.session_id,
                "timestamp": e.timestamp,
                "seq": e.seq,
                "payload": e.payload,
            }
            for e in state.recent_events(limit)
        ],
    })


@router.post("/api/events")
async def api_post_event(request: Request) -> JSONResponse:
    """
    注入一条事件 (测试 / 外部系统接入)

    W34: 写操作强制鉴权——由 middleware 全局拦截, 缺/无效 token 直接 401

    @note 实际生产中, WebState 由 SessionEvent bus 自动填充
    """
    state = _state(request)
    body = await request.json()
    await state.push_event(
        event_name=body.get("event_name", "unknown"),
        session_id=body.get("session_id", "manual"),
        seq=body.get("seq", 0),
        payload=body.get("payload", {}),
    )
    # W34: middleware 注入 user; 无 user 时 by="anonymous" (W28 行为兼容)
    user = getattr(request.state, "user", None)
    by = user.get("sub", "anonymous") if user else "anonymous"
    return JSONResponse({"ok": True, "by": by})


@router.get("/healthz")
async def healthz() -> JSONResponse:
    return JSONResponse({"status": "ok"})


@router.get("/metrics")
async def metrics(request: Request) -> Response:
    """Prometheus 指标代理 / stub (P5 扩展点)"""
    state = _state(request)
    lines: list[str] = []
    # 基础指标
    lines.append("# HELP agent_swarm_events_total Total events received")
    lines.append("# TYPE agent_swarm_events_total counter")
    for name, count in state.events_by_type().items():
        lines.append(f'agent_swarm_events_total{{name="{name}"}} {count}')
    lines.append("")
    lines.append("# HELP agent_swarm_active_sessions Active session count")
    lines.append("# TYPE agent_swarm_active_sessions gauge")
    lines.append(f"agent_swarm_active_sessions {state.session_count()}")
    lines.append("")
    lines.append("# HELP agent_swarm_uptime_seconds Web UI uptime")
    lines.append("# TYPE agent_swarm_uptime_seconds gauge")
    lines.append(f"agent_swarm_uptime_seconds {int(state.uptime_seconds())}")
    return Response(
        content="\n".join(lines),
        media_type="text/plain; version=0.0.4",
    )


# ---------------------------------------------------------------------------
# P5-W36b: agent_review Web 入口
# ---------------------------------------------------------------------------


@router.get("/review", response_class=HTMLResponse)
async def review_page(request: Request) -> HTMLResponse:
    """Review 页面 (HTMX 表单 + Run Review 按钮)"""
    tpl = _templates(request)
    return tpl.TemplateResponse(
        request,
        "review.html",
        {"page": "review"},
    )


@router.post("/api/review")
async def api_review(request: Request) -> JSONResponse:
    """
    调 agent_review.run_simple_review 同步返 ReviewReport

    W36b DoD:
      - 接受 pr_ref (default "main..HEAD")
      - 写路径强制 Bearer token (W34 middleware)
      - 错误处理: 无 git repo / 无效 pr_ref / git 异常 → 友好 JSON

    @note 实际生产中 agent_review 应异步, W36b 同步优先 (W13 决策)
    """
    # 解析 body (允许空 body → 默认 pr_ref)
    try:
        body: dict[str, Any] = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    except Exception:
        body = {}
    pr_ref = body.get("pr_ref", "main..HEAD")
    if not isinstance(pr_ref, str):
        return JSONResponse({"detail": "pr_ref must be a string"}, status_code=400)
    # 校验 pr_ref
    err = _validate_pr_ref(pr_ref)
    if err:
        return JSONResponse({"detail": err}, status_code=400)
    # 取 repo_root (W36b: 复用 worktree_manager 模式, 加 web_repo_root)
    web_repo_root: Path | None = getattr(request.app.state, "web_repo_root", None)
    # 调 agent_review (run_simple_review 是 sync, 用 to_thread 不阻塞 event loop)
    try:
        # 延迟导入避免循环
        from agent_swarm.web import review_runner

        report_dict: dict[str, Any] = await asyncio.to_thread(
            review_runner.run_review_sync,
            pr_ref,
            web_repo_root,
        )
    except FileNotFoundError as exc:
        # git 不在 PATH
        return JSONResponse(
            {"detail": f"git not available: {exc}"},
            status_code=500,
        )
    except RuntimeError as exc:
        # 非 git repo / 无 diff / git 异常
        msg = str(exc)
        if "not a git repository" in msg or "not a git" in msg:
            return JSONResponse(
                {"detail": "not a git repository", "hint": "configure --web-review-repo"},
                status_code=500,
            )
        if "no diff" in msg.lower() or "empty" in msg.lower():
            # 无变更 → 返空 report (200)
            return JSONResponse({
                "ok": True,
                "report": {
                    "pr_ref": pr_ref,
                    "verdict": "approve",
                    "findings": [],
                    "root_causes": [],
                    "summary": f"无变更 (pr_ref={pr_ref!r})",
                    "confidence": 1.0,
                },
            })
        return JSONResponse({"detail": f"review failed: {exc}"}, status_code=500)
    except Exception as exc:  # noqa: BLE001
        log.exception("agent_review failed")
        return JSONResponse(
            {"detail": f"unexpected error: {type(exc).__name__}: {exc}"},
            status_code=500,
        )
    return JSONResponse({"ok": True, "report": report_dict})


@router.get("/partials/review_form")
async def review_form_partial(request: Request) -> HTMLResponse:
    """Review 表单 partial (供 HTMX 加载)"""
    tpl = _templates(request)
    return tpl.TemplateResponse(
        request,
        "partials/review_form.html",
        {},
    )
