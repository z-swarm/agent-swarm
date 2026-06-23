"""
@module agent_swarm.web
@brief  P5-W28 GUI Web UI v1——HTMX + FastAPI

实时仪表盘: agents / worktrees / tasks + 事件流 + Prometheus 代理
P5-W33: 加 WebStateStore 抽象 + Postgres 持久化
P5-W34: 加 JWT 鉴权 (HS256, ${VAR} 引用)
"""

from agent_swarm.web.app import create_app
from agent_swarm.web.auth import (
    JWTConfig,
    JWTError,
    JWTIssuer,
    get_current_user,
    require_user,
    resolve_secret_ref,
)
from agent_swarm.web.state import WebState
from agent_swarm.web.store import (
    MemoryWebStateStore,
    PostgresNotifier,
    PostgresWebStateStore,
    WebStateConfig,
    WebStateStore,
)

__all__ = [
    "WebState",
    "WebStateStore",
    "WebStateConfig",
    "MemoryWebStateStore",
    "PostgresWebStateStore",
    "PostgresNotifier",
    "JWTConfig",
    "JWTError",
    "JWTIssuer",
    "get_current_user",
    "require_user",
    "resolve_secret_ref",
    "create_app",
]
