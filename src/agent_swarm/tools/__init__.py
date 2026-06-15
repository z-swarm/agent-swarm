"""
@module agent_swarm.tools
@brief  工具注册表——W1 仅 read_file
"""

from pathlib import Path

from agent_swarm.core.types import Tool
from agent_swarm.tools.builtin.file_ops import ReadFileTool


def build_default_tools(workspace: Path | str | None = None) -> dict[str, Tool]:
    """
    构造 W1 默认工具集

    @param workspace 工具的工作目录限制（W5 改为从 SecurityContext 取）
    @return 工具 id → Tool 实例的映射
    """
    rf = ReadFileTool(workspace=workspace)
    return {rf.name: rf}


__all__ = ["build_default_tools", "ReadFileTool"]
