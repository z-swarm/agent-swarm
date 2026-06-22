"""
@module agent_swarm.worktree.integration
@brief  P4-W23: Worktree ↔ MCP server 集成

提供:
  - substitute_placeholders(config, worktree_path): 把 MCPServerConfig 里的
    ${WORKTREE_PATH} 替换成实际 worktree 路径, 返回新 config
  - WorktreeIntegration: 高层封装, 用于 Swarm 启动时批量注入
"""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_swarm.mcp.registry import MCPServerConfig
    from agent_swarm.worktree.manager import WorktreeHandle, WorktreeManager

# 支持的占位符
PLACEHOLDER = "${WORKTREE_PATH}"
PLACEHOLDER_RE = re.compile(r"\$\{WORKTREE_PATH\}")


def substitute_placeholders(
    config: MCPServerConfig,
    worktree_path: Path,
) -> MCPServerConfig:
    """
    替换 MCPServerConfig 里的 ${WORKTREE_PATH} 占位符

    @param config        原始 MCP server 配置
    @param worktree_path 实际 worktree 路径
    @return 新 config (含替换后的 command / cwd / env)
    """
    path_str = str(worktree_path)
    # re.sub 把 replacement 里的 \ 当转义; 用 lambda 避免 escape
    repl = lambda _m: path_str  # noqa: E731

    new_command = [PLACEHOLDER_RE.sub(repl, arg) for arg in config.command]
    new_cwd = PLACEHOLDER_RE.sub(repl, config.cwd) if config.cwd else None
    new_env = {
        k: PLACEHOLDER_RE.sub(repl, v) for k, v in config.env.items()
    }
    return replace(
        config,
        command=new_command,
        cwd=new_cwd,
        env=new_env,
    )


def find_placeholders(config: MCPServerConfig) -> list[str]:
    """返回 config 中出现的占位符位置 (用于校验)"""
    found: list[str] = []
    for arg in config.command:
        if PLACEHOLDER in arg:
            found.append(f"command[{arg!r}]")
    if config.cwd and PLACEHOLDER in config.cwd:
        found.append(f"cwd={config.cwd!r}")
    for k, v in config.env.items():
        if PLACEHOLDER in v:
            found.append(f"env[{k}]={v!r}")
    return found


def validate_config(config: MCPServerConfig) -> None:
    """
    校验 MCPServerConfig 是否可注入 worktree (占位符存在, 或不需要)

    @raise ValueError 占位符在意外位置出现
    """
    # 占位符必须出现在 command 列表中, 或 cwd 中——不能在 token / url 中
    if PLACEHOLDER in (config.token or ""):
        raise ValueError(
            f"MCPServerConfig[{config.name!r}].token should not contain {PLACEHOLDER}"
        )
    if config.url and PLACEHOLDER in config.url:
        raise ValueError(
            f"MCPServerConfig[{config.name!r}].url should not contain {PLACEHOLDER}"
        )


class WorktreeIntegration:
    """
    高层封装: WorktreeManager + MCPRegistry 绑定

    用法:
        wt_integration = WorktreeIntegration(worktree_manager)
        for agent in agents:
            handle = wt_integration.acquire_for_agent(agent, security_ctx)
            # 后续启动 MCP server 时, 用 handle.path 替换占位符
    """

    def __init__(self, worktree_manager: WorktreeManager) -> None:
        self.wm = worktree_manager

    def acquire_for_agent(
        self,
        agent_id: str,
        tenant_id: str = "default",
        session_id: str = "default",
    ) -> WorktreeHandle:
        """为单个 agent 分配 worktree"""
        return self.wm.acquire(
            tenant_id=tenant_id,
            session_id=session_id,
            agent_id=agent_id,
        )

    def release_for_agent(self, handle: WorktreeHandle) -> None:
        self.wm.release(handle)

    def materialize_config(
        self,
        config: MCPServerConfig,
        handle: WorktreeHandle,
    ) -> MCPServerConfig:
        """把 config 注入到具体 worktree 路径"""
        validate_config(config)
        return substitute_placeholders(config, handle.path)


__all__ = [
    "PLACEHOLDER",
    "WorktreeIntegration",
    "find_placeholders",
    "substitute_placeholders",
    "validate_config",
]
