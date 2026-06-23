"""
@module agent_swarm.web
@brief  P5-W28 GUI Web UI v1——HTMX + FastAPI

实时仪表盘: agents / worktrees / tasks + 事件流 + Prometheus 代理
P5-W33: 加 WebStateStore 抽象 + Postgres 持久化
"""

from agent_swarm.web.app import create_app
from agent_swarm.web.state import WebState
from agent_swarm.web.store import (
    MemoryWebStateStore,
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
    "create_app",
]
