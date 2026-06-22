"""
@module agent_swarm.worktree
@brief  P4-W22: per-agent git worktree isolation

隔离策略:
  - 每个 agent (tenant, session, agent) 组合分配独立 git worktree
  - 工作树路径: `<base_dir>/wt-<tenant>-<session>-<agent>`
  - 分支名: `wt/<tenant>/<session>/<agent>` (避免碰撞, 易识别)
  - 同 (tenant, session, agent) acquire 幂等返回同一 handle
  - 异步安全: per-tenant asyncio.Lock
  - 清理: 延迟 (TTL) + 显式 release
"""

from agent_swarm.worktree.integration import (
    PLACEHOLDER,
    WorktreeIntegration,
    find_placeholders,
    substitute_placeholders,
    validate_config,
)
from agent_swarm.worktree.manager import WorktreeHandle, WorktreeManager

__all__ = [
    "PLACEHOLDER",
    "WorktreeHandle",
    "WorktreeIntegration",
    "WorktreeManager",
    "find_placeholders",
    "substitute_placeholders",
    "validate_config",
]
