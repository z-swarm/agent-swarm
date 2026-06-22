"""
@module agent_swarm.web.app
@brief  P5-W28 FastAPI app factory

用法:
    from agent_swarm.web import create_app
    app = create_app(web_state=WebState())
    uvicorn.run(app, host="0.0.0.0", port=8000)

@note 默认挂载所有 Phase 3-4 模块的 view
@note 测试用 TestClient; 不需要真 server
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from agent_swarm.web.routes import router as web_router
from agent_swarm.web.state import WebState
from agent_swarm.web.websocket import router as ws_router

log = logging.getLogger(__name__)

# 模板 + 静态资源路径
WEB_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"


def create_app(
    *,
    web_state: WebState | None = None,
    worktree_manager: Any = None,
    title: str = "agent-swarm",
    version: str = "0.5.0a1",
) -> FastAPI:
    """
    构造 FastAPI app

    @param web_state         Web UI 状态容器 (None = 新建)
    @param worktree_manager  可选 WorktreeManager (P5-W32: 注入后 /worktrees 页显真数据)
    @param title             app 标题 (OpenAPI docs)
    @param version           app 版本
    @return FastAPI 实例
    """
    state = web_state or WebState()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> Any:
        log.info("agent-swarm web ui started (uptime=0s)")
        yield
        log.info("agent-swarm web ui stopped")

    app = FastAPI(
        title=title,
        version=version,
        lifespan=lifespan,
    )
    # 挂状态
    app.state.web_state = state
    # 可选: WorktreeManager (P5-W32) — 路由用 getattr 兜底
    if worktree_manager is not None:
        app.state.worktree_manager = worktree_manager

    # 静态 + 模板
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    # 模板可访问 state / request.state.web_state
    # 简化: 在路由里用 app.state.web_state
    app.state.templates = templates

    # 路由
    app.include_router(web_router)
    app.include_router(ws_router)

    return app


__all__ = ["create_app"]
