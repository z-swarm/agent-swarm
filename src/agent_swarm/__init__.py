"""
@module agent_swarm
@brief  agent-swarm 顶层包

W1 切片导出：Swarm（CLI/SDK 入口）+ 关键数据类型
"""

from agent_swarm.core.swarm import Swarm
from agent_swarm.core.types import (
    Agent,
    AgentCapabilities,
    Task,
    Turn,
)

__version__ = "0.5.0"

__all__ = [
    "Swarm",
    "Agent",
    "AgentCapabilities",
    "Task",
    "Turn",
    "__version__",
]
