"""
@module agent_swarm.mcp
@brief  W9/W14a MCP（Model Context Protocol）集成——DESIGN §7.3

W9 范围：stdio 传输 + 注册表 + 适配器
W14a 范围：SSE 传输 + 可靠性（重连 + 熔断 + circuit breaker）

进度：W9-1 → W9-2 → W9-3 ✅;W9-4 (SSE) → W9-5 (重连/熔断) ✅（在 W14a 落地）;
      W9-6 (接 2 server) ✅;W9-7 (e2e + DoD) ✅
"""

from agent_swarm.mcp.adapter import MCPToolAdapter, await_build_tool_adapters
from agent_swarm.mcp.client import (
    MCPClient,
    MCPConnectionError,
    MCPError,
    MCPRPCError,
    MCPTimeoutError,
    StdioMCPClient,
)
from agent_swarm.mcp.registry import MCPHealthStatus, MCPRegistry, MCPServerConfig
from agent_swarm.mcp.reliability import (
    CircuitBreaker,
    CircuitState,
    MCPCircuitOpenError,
    ReconnectingMCPClient,
)
from agent_swarm.mcp.sse import MCPHTTPError, SseMCPClient

__all__ = [
    "CircuitBreaker",
    "CircuitState",
    "MCPClient",
    "MCPConnectionError",
    "MCPError",
    "MCPHealthStatus",
    "MCPHTTPError",
    "MCPRPCError",
    "MCPRegistry",
    "MCPRPCError",
    "MCPServerConfig",
    "MCPCircuitOpenError",
    "MCPTimeoutError",
    "MCPToolAdapter",
    "ReconnectingMCPClient",
    "SseMCPClient",
    "StdioMCPClient",
    "await_build_tool_adapters",
]
