"""
@module agent_swarm.web.app
@brief  P5-W28 FastAPI app factory

用法:
    from agent_swarm.web import create_app
    app = create_app(web_state=WebState())
    uvicorn.run(app, host="0.0.0.0", port=8000)

P5-W33: create_app 接受 postgres_dsn (None = 内存, 零破坏)
        DSN 给出时自动实例化 PostgresWebStateStore 注入到 WebState
P5-W34: create_app 接受 jwt_secret (None = 无鉴权, 零破坏)
        给出时挂 JWTIssuer + middleware 解析 Authorization: Bearer
        关键 API 路由用 Depends(require_user) 强制鉴权

@note 默认挂载所有 Phase 3-4 模块的 view
@note 测试用 TestClient; 不需要真 server
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
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
    postgres_dsn: str | None = None,
    postgres_table: str = "webstate_events",
    postgres_tenant_id: str = "local",
    jwt_secret: str | None = None,
    jwt_algorithm: str = "HS256",
    jwt_expires_seconds: int = 3600,
    jwt_issuer_name: str = "agent-swarm",
    title: str = "agent-swarm",
    version: str = "0.5.0a1",
) -> FastAPI:
    """
    构造 FastAPI app

    @param web_state         Web UI 状态容器 (None = 新建)
    @param worktree_manager  可选 WorktreeManager (P5-W32: 注入后 /worktrees 页显真数据)
    @param postgres_dsn      W33: Postgres DSN (None = 内存 store, 零破坏)
    @param postgres_table    W33: 表名 (默认 webstate_events)
    @param postgres_tenant_id W33: tenant_id 列默认值 (多租户隔离)
    @param jwt_secret        W34: HS256 共享密钥 (None = 无鉴权, 零破坏; ${VAR} 引用支持)
    @param jwt_algorithm     W34: 算法 (固定 HS256)
    @param jwt_expires_seconds W34: token 有效期
    @param jwt_issuer_name   W34: iss 字段
    @param title             app 标题 (OpenAPI docs)
    @param version           app 版本
    @return FastAPI 实例
    """
    state = web_state or WebState()
    # W33: DSN 给出时挂 Postgres store
    if postgres_dsn and state.store is None:
        from agent_swarm.web.store import PostgresWebStateStore, WebStateConfig
        state.store = PostgresWebStateStore(WebStateConfig(
            dsn=postgres_dsn,
            table=postgres_table,
            tenant_id=postgres_tenant_id,
        ))
        log.info("WebState Postgres store attached: table=%s", postgres_table)
    # W34: secret 给出时挂 JWT issuer
    jwt_issuer_obj: Any = None
    if jwt_secret:
        from agent_swarm.web.auth import JWTConfig, JWTIssuer, resolve_secret_ref
        resolved = resolve_secret_ref(jwt_secret)
        jwt_issuer_obj = JWTIssuer(JWTConfig(
            secret=resolved,
            algorithm=jwt_algorithm,
            expires_seconds=jwt_expires_seconds,
            issuer=jwt_issuer_name,
        ))
        log.info("WebState JWT auth enabled: issuer=%s", jwt_issuer_name)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> Any:
        log.info("agent-swarm web ui started (uptime=0s)")
        # W33: 启动时确保 store 已就绪 (无 DSN 时跳过)
        if state.store is not None and hasattr(state.store, "_ensure_connected"):
            try:
                await state.store._ensure_connected()
            except Exception as exc:  # noqa: BLE001
                log.warning("WebState store init failed: %s", exc)
        yield
        # W33: 退出时关 store
        if state.store is not None:
            try:
                await state.store.close()
            except Exception as exc:  # noqa: BLE001
                log.debug("WebState store close failed: %s", exc)
        log.info("agent-swarm web ui stopped")

    app = FastAPI(
        title=title,
        version=version,
        lifespan=lifespan,
    )
    # 挂状态
    app.state.web_state = state
    # W34: JWT issuer
    app.state.jwt_issuer = jwt_issuer_obj
    # 可选: WorktreeManager (P5-W32) — 路由用 getattr 兜底
    if worktree_manager is not None:
        app.state.worktree_manager = worktree_manager

    # W34: JWT 解析 + 写路径强制鉴权 middleware (secret 未配置时不挂)
    if jwt_issuer_obj is not None:
        from fastapi.responses import JSONResponse

        from agent_swarm.web.auth import JWTError

        # 强制鉴权的写路径前缀 (W34 决策: 不在路由签名里加 Depends, 避开 FastAPI 422 解析坑)
        PROTECTED_PREFIXES = ("/api/events",)

        @app.middleware("http")
        async def jwt_middleware(request: Request, call_next: Any) -> Any:
            """解析 Authorization: Bearer → 注入 request.state.user; 写路径无 token 直接 401"""
            auth = request.headers.get("Authorization", "")
            user: Any = None
            if auth.startswith("Bearer "):
                token = auth[7:].strip()
                if token:
                    try:
                        user = jwt_issuer_obj.decode(token)
                    except JWTError as exc:
                        log.debug("JWT decode failed: %s", exc)
                        user = None
            request.state.user = user
            # 写路径 + 缺 user → 401 (POST/PUT/DELETE/PATCH)
            method = request.method.upper()
            if method in ("POST", "PUT", "DELETE", "PATCH") and user is None:
                path = request.url.path
                if any(path.startswith(p) for p in PROTECTED_PREFIXES):
                    return JSONResponse(
                        {"detail": "unauthorized"},
                        status_code=401,
                    )
            return await call_next(request)

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
