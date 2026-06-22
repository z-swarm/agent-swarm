"""
@module agent_swarm.web
@brief  P5-W28 GUI Web UI v1——HTMX + FastAPI

实时仪表盘: agents / worktrees / tasks + 事件流 + Prometheus 代理
"""

from agent_swarm.web.app import create_app
from agent_swarm.web.state import WebState

__all__ = ["WebState", "create_app"]
