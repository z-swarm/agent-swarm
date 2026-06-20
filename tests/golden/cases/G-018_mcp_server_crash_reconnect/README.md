# G-018 Golden Case

**场景**: MCP server 崩溃 → 客户端 3 次重连失败 → 熔断 → agent 收到 ToolUnavailableError

**对应**: DESIGN §17.3 G-018 + §7.3 "MCP 可靠性策略"

**W14a 落地**: 
- `src/agent_swarm/mcp/reliability.py` CircuitBreaker + ReconnectingMCPClient
- `tests/unit/test_mcp_circuit_breaker.py` (12 测试)
- `tests/unit/test_mcp_resilience.py` (14 测试)
- `tools/count_reconnect.py` 验证脚本

**验收**:
- `pytest tests/golden/test_golden_p2.py -k g018` 通过
- `tools/count_reconnect.py` 输出 reconnect 次数 + circuit state 转移
