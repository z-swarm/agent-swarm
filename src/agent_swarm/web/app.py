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
P5-W36a: create_app 接受 secret_manager + jwt_secret_ref (SecretManager 集成, 支持轮换)
         secret_manager 缺省 → 自动实例化 EnvSecretManager

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
    web_repo_root: Path | None = None,
    postgres_dsn: str | None = None,
    postgres_table: str = "webstate_events",
    postgres_tenant_id: str = "local",
    enable_cross_process: bool = False,
    jwt_secret: str | None = None,
    jwt_secret_ref: str | None = None,
    secret_manager: Any = None,
    jwt_algorithm: str = "HS256",
    jwt_expires_seconds: int = 3600,
    jwt_issuer_name: str = "agent-swarm",
    vault_url: str = "http://127.0.0.1:8200",
    vault_role_id: str | None = None,
    vault_secret_id: str | None = None,
    review_mode: str = "full",
    review_llm: str = "fake",
    review_timeout: float = 60.0,
    title: str = "agent-swarm",
    version: str = "0.5.0a2",
) -> FastAPI:
    """
    构造 FastAPI app

    @param web_state         Web UI 状态容器 (None = 新建)
    @param worktree_manager  可选 WorktreeManager (P5-W32: 注入后 /worktrees 页显真数据)
    @param web_repo_root     W36b: git 仓库根 (用于 /api/review 跑 agent_review; None = 用 cwd)
    @param postgres_dsn      W33: Postgres DSN (None = 内存 store, 零破坏)
    @param postgres_table    W33: 表名 (默认 webstate_events)
    @param postgres_tenant_id W33: tenant_id 列默认值 (多租户隔离)
    @param enable_cross_process W35: 启用跨进程 LISTEN/NOTIFY fan-out
                                  (需要 postgres_dsn + fake_module 之一; DSN 缺省时无效)
    @param jwt_secret        W34: HS256 共享密钥字面值 / ${VAR} 引用 (None = 无鉴权, 零破坏)
    @param jwt_secret_ref    W36a: secret 引用字符串 (literal / ${VAR} / secret://key)
                              与 jwt_secret 互斥; 与 secret_manager 配合支持轮换
    @param secret_manager    W36a: SecretManager 实例 (None = 自动 EnvSecretManager, 仅 W36a 模式)
    @param jwt_algorithm     W34: 算法 (固定 HS256)
    @param jwt_expires_seconds W34: token 有效期
    @param jwt_issuer_name   W34: iss 字段
    @param vault_url         W36c: Vault URL (vault:// 模式自动实例化时使用)
    @param vault_role_id     W36c: Vault AppRole role_id (vault:// 模式自动实例化时使用)
    @param vault_secret_id   W36c: Vault AppRole secret_id (vault:// 模式自动实例化时使用)
    @param review_mode       W36f: agent_review 模式 (simple / full; 默认 full)
    @param review_llm        W36f: full mode LLM provider (openai / anthropic / fake; 默认 fake)
    @param review_timeout    W36f: full mode LLM 调用超时 (秒, 默认 60)
    @param title             app 标题 (OpenAPI docs)
    @param version           app 版本
    @return FastAPI 实例
    @raise ValueError jwt_secret 与 jwt_secret_ref 同时给出
    """
    if jwt_secret is not None and jwt_secret_ref is not None:
        raise ValueError(
            "jwt_secret and jwt_secret_ref are mutually exclusive: "
            "use jwt_secret (W34 字面值) or jwt_secret_ref (W36a SecretManager)"
        )
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
    # W34/W36a: 挂 JWT issuer
    jwt_issuer_obj: Any = None
    if jwt_secret or jwt_secret_ref:
        from agent_swarm.web.auth import (
            JWTConfig,
            JWTIssuer,
            parse_secret_ref,
            resolve_secret_ref,
        )
        if jwt_secret is not None:
            # W34 模式: 字面值 / ${VAR} 一次性 resolve
            resolved = resolve_secret_ref(jwt_secret)
            jwt_issuer_obj = JWTIssuer(JWTConfig(
                secret=resolved,
                algorithm=jwt_algorithm,
                expires_seconds=jwt_expires_seconds,
                issuer=jwt_issuer_name,
            ))
            log.info("WebState JWT auth enabled (W34 mode): issuer=%s", jwt_issuer_name)
        else:
            # W36a 模式: SecretRef 协议 + SecretManager
            assert jwt_secret_ref is not None  # 上层已校验
            ref = parse_secret_ref(jwt_secret_ref)
            # secret:// / vault:// 模式: 必须有 SecretManager
            if ref.kind == "secret_ref":
                if secret_manager is None:
                    # 缺省: EnvSecretManager (W20 风格, 与 W34 ${VAR} 兼容路径同源)
                    from agent_swarm.security.secret_manager import EnvSecretManager
                    secret_manager = EnvSecretManager()
                    log.info("WebState JWT auth: default EnvSecretManager attached")
                jwt_issuer_obj = JWTIssuer(JWTConfig(
                    secret_ref=jwt_secret_ref,
                    secret_manager=secret_manager,
                    algorithm=jwt_algorithm,
                    expires_seconds=jwt_expires_seconds,
                    issuer=jwt_issuer_name,
                ))
                # W36a 模式: lifespan 启动时 await resolve_secret() 初始化 cache
                # 失败仅 log, 不破 (降级路径: cache miss 时 middleware 仍跑)
                log.info(
                    "WebState JWT auth enabled (W36a mode): ref=%s issuer=%s",
                    jwt_secret_ref, jwt_issuer_name,
                )
            elif ref.kind == "vault":
                # W36c: vault://path#field 模式
                if secret_manager is None:
                    # 缺省: VaultSecretManager (需 vault_url/role_id/secret_id)
                    from agent_swarm.security.secret_manager import (
                        VaultConfig,
                        VaultSecretManager,
                    )
                    secret_manager = VaultSecretManager(VaultConfig(
                        url=vault_url,
                        role_id=vault_role_id or "",
                        secret_id=vault_secret_id or "",
                    ))
                    log.info(
                        "WebState JWT auth: default VaultSecretManager attached url=%s",
                        vault_url,
                    )
                jwt_issuer_obj = JWTIssuer(JWTConfig(
                    secret_ref=jwt_secret_ref,
                    secret_manager=secret_manager,
                    algorithm=jwt_algorithm,
                    expires_seconds=jwt_expires_seconds,
                    issuer=jwt_issuer_name,
                ))
                log.info(
                    "WebState JWT auth enabled (W36c vault mode): ref=%s issuer=%s",
                    jwt_secret_ref, jwt_issuer_name,
                )
            elif ref.kind == "env":
                # ${VAR} 通过 secret_ref 字段: 一次性 resolve 进 secret
                import os
                env_val = os.environ.get(ref.value)
                if env_val is None:
                    raise ValueError(
                        f"env var {ref.value!r} not set (referenced by {jwt_secret_ref!r})"
                    )
                jwt_issuer_obj = JWTIssuer(JWTConfig(
                    secret=env_val,
                    algorithm=jwt_algorithm,
                    expires_seconds=jwt_expires_seconds,
                    issuer=jwt_issuer_name,
                ))
                log.info(
                    "WebState JWT auth enabled (env ref %s): issuer=%s",
                    ref.value, jwt_issuer_name,
                )
            else:  # literal
                jwt_issuer_obj = JWTIssuer(JWTConfig(
                    secret=ref.value,
                    algorithm=jwt_algorithm,
                    expires_seconds=jwt_expires_seconds,
                    issuer=jwt_issuer_name,
                ))
                log.info("WebState JWT auth enabled (literal ref): issuer=%s", jwt_issuer_name)
    # W35: 跨进程 fan-out — 需要 DSN 才有 Postgres store
    notifier: Any = None
    if enable_cross_process and postgres_dsn:
        from agent_swarm.web.store import PostgresNotifier
        notifier = PostgresNotifier(dsn=postgres_dsn)
        log.info("WebState cross-process fan-out enabled: origin=%s", notifier.origin_id[:8])

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> Any:
        log.info("agent-swarm web ui started (uptime=0s)")
        # W33: 启动时确保 store 已就绪 (无 DSN 时跳过)
        if state.store is not None and hasattr(state.store, "_ensure_connected"):
            try:
                await state.store._ensure_connected()
            except Exception as exc:  # noqa: BLE001
                log.warning("WebState store init failed: %s", exc)
        # W35: 启动 notifier + 挂到 state (失败仅 log, 不破坏单进程路径)
        if notifier is not None:
            try:
                await notifier.listen()
                state.attach_notifier(notifier)
                log.info("WebState cross-process notifier active")
            except Exception as exc:  # noqa: BLE001
                log.warning("WebState notifier listen failed: %s", exc)
        # W36a: 启动时 await resolve_secret 初始化 cache (失败仅 log, 不破)
        if (
            jwt_issuer_obj is not None
            and jwt_issuer_obj.config.secret is None
            and jwt_issuer_obj.config.secret_manager is not None
        ):
            try:
                await jwt_issuer_obj.resolve_secret()
                log.info("WebState JWT cache initialized (W36a mode)")
            except Exception as exc:  # noqa: BLE001
                log.warning("WebState JWT cache init failed (degrade): %s", exc)
        yield
        # W35: 退出时关 notifier
        if notifier is not None:
            try:
                await notifier.close()
            except Exception as exc:  # noqa: BLE001
                log.debug("WebState notifier close failed: %s", exc)
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
    # W34/W36a: JWT issuer
    app.state.jwt_issuer = jwt_issuer_obj
    # W35: notifier
    app.state.web_notifier = notifier
    # W36b: web_repo_root (review 路由读)
    if web_repo_root is not None:
        app.state.web_repo_root = web_repo_root
    # W36f: review 模式配置 (mode / llm / timeout)
    app.state.web_review_mode = review_mode
    app.state.web_review_llm = review_llm
    app.state.web_review_timeout = review_timeout
    # 可选: WorktreeManager (P5-W32) — 路由用 getattr 兜底
    if worktree_manager is not None:
        app.state.worktree_manager = worktree_manager

    # W34/W36a: JWT 解析 + 写路径强制鉴权 middleware (issuer 未配置时不挂)
    if jwt_issuer_obj is not None:
        from fastapi.responses import JSONResponse

        from agent_swarm.web.auth import JWTError

        # 强制鉴权的写路径前缀 (W34 决策: 不在路由签名里加 Depends, 避开 FastAPI 422 解析坑)
        PROTECTED_PREFIXES = ("/api/events", "/api/review")

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
