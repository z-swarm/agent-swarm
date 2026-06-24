"""
@module agent_swarm.web
@brief  P5-W28 GUI Web UI v1——HTMX + FastAPI

实时仪表盘: agents / worktrees / tasks + 事件流 + Prometheus 代理
P5-W33: 加 WebStateStore 抽象 + Postgres 持久化
P5-W34: 加 JWT 鉴权 (HS256, ${VAR} 引用)
P5-W36a: 加 SecretRef 协议 (literal / ${VAR} / secret://key) + SecretManager 集成
P5-W41: 加 app_factory (uvicorn workers=N factory 模式)
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from agent_swarm.web.app import create_app
from agent_swarm.web.auth import (
    JWTConfig,
    JWTError,
    JWTIssuer,
    SecretRef,
    get_current_user,
    parse_secret_ref,
    require_user,
    resolve_secret_ref,
)
from agent_swarm.web.review_runner import (
    MemoryTaskStore,
    RedisTaskStore,
    TaskStore,
    create_task_store,
)
from agent_swarm.web.state import WebState
from agent_swarm.web.store import (
    MemoryWebStateStore,
    PostgresNotifier,
    PostgresWebStateStore,
    WebStateConfig,
    WebStateStore,
)


def app_factory() -> Any:
    """
    @brief W41: uvicorn factory 模式入口 (无参, 从 env 读配置)

    配置从环境变量读 (uvicorn workers=N fork 子进程, 子进程内无 CLI kwargs 传):
      WEB_POSTGRES_DSN / WEB_POSTGRES_TABLE / WEB_POSTGRES_TENANT
      WEB_CROSS_PROCESS (0/1)
      WEB_JWT_SECRET / WEB_JWT_SECRET_REF
      WEB_REVIEW_MODE / WEB_REVIEW_LLM / WEB_REVIEW_TIMEOUT
      WEB_TASK_STORE / WEB_REDIS_DSN
      WEB_WORKTREE_REPO / WEB_WORKTREE_BASE
    @return FastAPI app 实例
    @note  缺省全 None = 走 W28 单进程内存路径 (零破坏)
    """
    pg_dsn = os.environ.get("WEB_POSTGRES_DSN") or None
    pg_table = os.environ.get("WEB_POSTGRES_TABLE") or "webstate_events"
    pg_tenant = os.environ.get("WEB_POSTGRES_TENANT") or "local"
    cross_process = os.environ.get("WEB_CROSS_PROCESS", "0") == "1"
    jwt_secret = os.environ.get("WEB_JWT_SECRET") or None
    jwt_secret_ref = os.environ.get("WEB_JWT_SECRET_REF") or None
    review_mode = os.environ.get("WEB_REVIEW_MODE") or "full"
    review_llm = os.environ.get("WEB_REVIEW_LLM") or "fake"
    review_timeout = float(os.environ.get("WEB_REVIEW_TIMEOUT") or "60.0")
    task_store_backend = os.environ.get("WEB_TASK_STORE") or "memory"
    redis_dsn = os.environ.get("WEB_REDIS_DSN") or None
    worktree_repo = os.environ.get("WEB_WORKTREE_REPO") or None
    worktree_base = os.environ.get("WEB_WORKTREE_BASE") or None
    worktree_manager: Any = None
    if worktree_repo:
        try:
            from agent_swarm.worktree import WorktreeManager  # noqa: E402

            base = Path(worktree_base) if worktree_base else (Path(worktree_repo) / ".worktrees")
            worktree_manager = WorktreeManager(
                repo_root=Path(worktree_repo),
                base_dir=base,
            )
        except ImportError:
            worktree_manager = None
    task_store = create_task_store(task_store_backend, redis_dsn)
    return create_app(
        postgres_dsn=pg_dsn,
        postgres_table=pg_table,
        postgres_tenant_id=pg_tenant,
        enable_cross_process=cross_process,
        jwt_secret=jwt_secret,
        jwt_secret_ref=jwt_secret_ref,
        review_mode=review_mode,
        review_llm=review_llm,
        review_timeout=review_timeout,
        task_store=task_store,
        worktree_manager=worktree_manager,
    )


__all__ = [
    "WebState",
    "WebStateStore",
    "WebStateConfig",
    "MemoryWebStateStore",
    "PostgresWebStateStore",
    "PostgresNotifier",
    "TaskStore",
    "MemoryTaskStore",
    "RedisTaskStore",
    "create_task_store",
    "JWTConfig",
    "JWTError",
    "JWTIssuer",
    "SecretRef",
    "get_current_user",
    "parse_secret_ref",
    "require_user",
    "resolve_secret_ref",
    "create_app",
    "app_factory",
]
