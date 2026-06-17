"""
@module agent_swarm.tui
@brief  Textual TUI 仪表盘（W6）

DESIGN.md §17.1 W6 DoD: TUI 启动后 5 秒内显示完整 swarm 视图

设计要点:
  - TUISink: ObservabilitySink 实现, 把事件投递到 asyncio.Queue
  - SwarmDashboardApp: 4 面板 Grid 布局 (Status / Tasks / Messages / Budget)
  - 反应式数据：事件驱动 → 面板自动重渲染
  - KISS: 单 process / 单 App / 不依赖外部进程或 IPC
"""

from agent_swarm.tui.app import SwarmDashboardApp, run_dashboard
from agent_swarm.tui.sink import TUISink

__all__ = ["SwarmDashboardApp", "TUISink", "run_dashboard"]
