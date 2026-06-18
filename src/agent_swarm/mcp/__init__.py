"""
@module agent_swarm.mcp
@brief  W9 MCP（Model Context Protocol）集成——DESIGN §7.3

按 Phase 2 节奏：W9-1 注册表 → W9-2 stdio 客户端 → W9-3 工具适配器
→ W9-4 SSE 客户端 → W9-5 重连/熔断 → W9-6 接 2 server → W9-7 e2e + DoD
"""

from agent_swarm.mcp.registry import MCPRegistry, MCPServerConfig

__all__ = ["MCPRegistry", "MCPServerConfig"]
