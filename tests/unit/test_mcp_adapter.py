"""单元测试：MCPToolAdapter（W9-3）"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from agent_swarm.mcp.adapter import (
    MCPToolAdapter,
    _serialize_content,
    await_build_tool_adapters,
    build_tool_adapters,
)
from agent_swarm.mcp.client import StdioMCPClient
from agent_swarm.mcp.registry import MCPServerConfig

_FAKE_MCP_SCRIPT = r"""
import sys, json

def handle(req):
    method = req.get("method")
    req_id = req.get("id")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": req_id, "result": {
            "protocolVersion": "2024-11-05",
            "serverInfo": {"name": "fake-mcp", "version": "0.0.1"},
        }}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": [{
            "name": "echo", "description": "echo back content",
            "inputSchema": {"type": "object", "properties": {"content": {"type": "string"}}, "required": ["content"]},
        }, {
            "name": "create_issue", "description": "create an issue",
            "inputSchema": {"type": "object", "properties": {"title": {"type": "string"}}},
        }]}}
    if method == "tools/call":
        name = req.get("params", {}).get("name")
        args = req.get("params", {}).get("arguments", {})
        if name == "echo":
            return {"jsonrpc": "2.0", "id": req_id, "result": {
                "content": [{"type": "text", "text": args.get("content", "")}],
            }}
        if name == "create_issue":
            return {"jsonrpc": "2.0", "id": req_id, "result": {
                "content": [{"type": "text", "text": f"created: {args.get('title', '')}"}],
            }}
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": "nope"}}

for line in sys.stdin:
    line = line.strip()
    if not line: continue
    req = json.loads(line)
    print(json.dumps(handle(req)), flush=True)
"""


@pytest.fixture
def fake_mcp_script(tmp_path: Path) -> Path:
    p = tmp_path / "fake_mcp.py"
    p.write_text(_FAKE_MCP_SCRIPT, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# _serialize_content
# ---------------------------------------------------------------------------


def test_serialize_content_none_returns_empty() -> None:
    assert _serialize_content(None) == ""


def test_serialize_content_string_returns_as_is() -> None:
    assert _serialize_content("hello") == "hello"


def test_serialize_content_text_blocks_joined() -> None:
    content = [
        {"type": "text", "text": "line1"},
        {"type": "text", "text": "line2"},
    ]
    assert _serialize_content(content) == "line1\nline2"


def test_serialize_content_mixed_text_and_json() -> None:
    content = [
        {"type": "text", "text": "before"},
        {"type": "image", "data": "..."},
    ]
    out = _serialize_content(content)
    assert "before" in out
    assert '"type": "image"' in out


# ---------------------------------------------------------------------------
# MCPToolAdapter
# ---------------------------------------------------------------------------


def test_adapter_name_has_server_prefix() -> None:
    """Tool.name 加 server 前缀避免跨 server 冲突（DESIGN §7.3）"""
    # 不真连 client，构造 stub
    class _StubClient:
        async def call_tool(self, name, args):
            return [{"type": "text", "text": "ok"}]

    a = MCPToolAdapter(
        server_name="github", mcp_tool_name="create_issue",
        description="x", parameters={"type": "object"},
        client=_StubClient(), risk="high",
    )
    assert a.name == "mcp.github.create_issue"
    assert a.risk == "high"


@pytest.mark.asyncio
async def test_adapter_invoke_serializes_content(fake_mcp_script: Path) -> None:
    cfg = MCPServerConfig(name="fake", transport="stdio",
                          command=[sys.executable, str(fake_mcp_script)])
    client = StdioMCPClient(cfg, timeout_s=5.0)
    adapters = await await_build_tool_adapters("fake", cfg, client)
    echo = next(a for a in adapters if a.mcp_tool_name == "echo")
    out = await echo.invoke({"content": "hello adapter"})
    assert out == "hello adapter"
    await client.disconnect()


# ---------------------------------------------------------------------------
# await_build_tool_adapters
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_adapters_connects_and_wraps_tools(fake_mcp_script: Path) -> None:
    cfg = MCPServerConfig(name="fake", transport="stdio",
                          command=[sys.executable, str(fake_mcp_script)])
    client = StdioMCPClient(cfg, timeout_s=5.0)
    adapters = await await_build_tool_adapters("fake", cfg, client)
    assert len(adapters) == 2
    mcp_names = {a.mcp_tool_name for a in adapters}
    assert mcp_names == {"echo", "create_issue"}
    # create_issue 风险等级覆写
    adapters_risky = await await_build_tool_adapters(
        "fake", cfg, client, risk_overrides={"create_issue": "high"},
    )
    issue = next(a for a in adapters_risky if a.mcp_tool_name == "create_issue")
    assert issue.risk == "high"
    await client.disconnect()


@pytest.mark.asyncio
async def test_build_adapters_skips_tools_without_name(fake_mcp_script: Path) -> None:
    """MCP tool 缺 name → 跳过（fail-soft）"""
    cfg = MCPServerConfig(name="fake", transport="stdio",
                          command=[sys.executable, str(fake_mcp_script)])
    StdioMCPClient(cfg, timeout_s=5.0)
    # 用 stub list_tools 模拟空 name
    class _StubClient:
        async def list_tools(self):
            return [{"description": "no name"}, {"name": "valid", "description": "x",
                                                   "inputSchema": {}}]
        def is_connected(self): return True
        async def connect(self): pass
    stub = _StubClient()
    adapters = await await_build_tool_adapters("fake", cfg, stub)  # type: ignore[arg-type]
    assert len(adapters) == 1
    assert adapters[0].mcp_tool_name == "valid"


# ---------------------------------------------------------------------------
# P1-3.1 (REVIEW-2026-06-19 §3.1) SecurityPolicy 链路 + risk_overrides 二次闸门
# ---------------------------------------------------------------------------


def _stub_client_with_call_log() -> tuple[object, list[tuple[str, dict]]]:
    """
    返回 (_StubClient, _calls)
    _calls 记录所有 (tool_name, arguments)，用于断言 client.call_tool 未被调
    """
    calls: list[tuple[str, dict]] = []

    class _StubClient:
        async def call_tool(self, name, args):
            calls.append((name, args))
            return [{"type": "text", "text": "stub-result"}]

        async def list_tools(self):
            return []

        def is_connected(self):
            return True

        async def connect(self):
            pass

    return _StubClient(), calls


@pytest.mark.asyncio
async def test_adapter_policy_deny_blocks_mcp_call() -> None:
    """policy.check_tool → DENY：invoke 返回 error，client.call_tool 不被调"""
    from agent_swarm.security import SecurityPolicy
    from agent_swarm.security.policy import PolicyDecision

    client, calls = _stub_client_with_call_log()
    a = MCPToolAdapter(
        server_name="github", mcp_tool_name="create_issue",
        description="x", parameters={"type": "object"},
        client=client, risk="low",  # 低风险 → 风险闸门不会拦
    )

    class _MockPolicy(SecurityPolicy):
        def check_tool(self, tool_name, arguments):  # type: ignore[override]
            return PolicyDecision("DENY", f"mock deny {tool_name}")

    a.policy = _MockPolicy()
    out = await a.invoke({"title": "test"})
    assert "[error]" in out
    assert "policy denied" in out
    assert "mock deny" in out
    assert calls == [], f"client.call_tool 仍被调用了: {calls}"


@pytest.mark.asyncio
async def test_adapter_policy_require_approval_blocks_mcp_call() -> None:
    """policy.check_tool → REQUIRE_APPROVAL：invoke 返回 error，client 不被调"""
    from agent_swarm.security import SecurityPolicy
    from agent_swarm.security.policy import PolicyDecision

    client, calls = _stub_client_with_call_log()
    a = MCPToolAdapter(
        server_name="github", mcp_tool_name="create_issue",
        description="x", parameters={"type": "object"},
        client=client, risk="low",
    )

    class _MockPolicy(SecurityPolicy):
        def check_tool(self, tool_name, arguments):  # type: ignore[override]
            return PolicyDecision("REQUIRE_APPROVAL", "mock approval")

    a.policy = _MockPolicy()
    out = await a.invoke({"title": "test"})
    assert "requires approval" in out
    assert calls == [], f"client.call_tool 仍被调用了: {calls}"


@pytest.mark.asyncio
async def test_adapter_policy_allow_proceeds_to_mcp_call() -> None:
    """policy.check_tool → ALLOW + 低风险 → 实际调 client.call_tool"""
    from agent_swarm.security import SecurityPolicy
    from agent_swarm.security.policy import PolicyDecision

    client, calls = _stub_client_with_call_log()
    a = MCPToolAdapter(
        server_name="github", mcp_tool_name="create_issue",
        description="x", parameters={"type": "object"},
        client=client, risk="low",
    )

    class _MockPolicy(SecurityPolicy):
        def check_tool(self, tool_name, arguments):  # type: ignore[override]
            return PolicyDecision("ALLOW", "ok")

    a.policy = _MockPolicy()
    out = await a.invoke({"title": "test"})
    assert out == "stub-result"
    assert calls == [("create_issue", {"title": "test"})]


@pytest.mark.asyncio
async def test_adapter_high_risk_blocks_even_when_policy_allows() -> None:
    """risk_overrides=high + policy ALLOW → 仍 REQUIRE_APPROVAL（防御深度）"""
    from agent_swarm.security import SecurityPolicy
    from agent_swarm.security.policy import PolicyDecision

    client, calls = _stub_client_with_call_log()
    a = MCPToolAdapter(
        server_name="github", mcp_tool_name="create_issue",
        description="x", parameters={"type": "object"},
        client=client, risk="high",  # YAML risk_overrides=high
    )

    class _MockPolicy(SecurityPolicy):
        def check_tool(self, tool_name, arguments):  # type: ignore[override]
            return PolicyDecision("ALLOW", "policy ok")

    a.policy = _MockPolicy()
    out = await a.invoke({"title": "x"})
    assert "requires approval" in out
    assert "risk=high" in out
    assert calls == [], "high 风险工具不应调用 MCP"


@pytest.mark.asyncio
async def test_adapter_critical_risk_blocks_even_when_policy_allows() -> None:
    """risk_overrides=critical → REQUIRE_APPROVAL"""
    client, calls = _stub_client_with_call_log()
    a = MCPToolAdapter(
        server_name="github", mcp_tool_name="create_issue",
        description="x", parameters={"type": "object"},
        client=client, risk="critical",
    )
    out = await a.invoke({"title": "x"})
    assert "requires approval" in out
    assert "risk=critical" in out
    assert calls == []


@pytest.mark.asyncio
async def test_adapter_no_policy_keeps_backward_compat() -> None:
    """无 policy 注入时（旧测试 + Phase 1 路径）按 risk 走闸门——保持向后兼容"""
    client, calls = _stub_client_with_call_log()
    a = MCPToolAdapter(
        server_name="github", mcp_tool_name="create_issue",
        description="x", parameters={"type": "object"},
        client=client, risk="medium",  # 中等风险 → 放行
    )
    assert a.policy is None
    out = await a.invoke({"title": "x"})
    assert out == "stub-result"
    assert calls == [("create_issue", {"title": "x"})]


@pytest.mark.asyncio
async def test_build_adapters_passes_policy_through() -> None:
    """build_tool_adapters(..., policy=...) → 每个 adapter 都拿到同一 policy"""
    from agent_swarm.security import SecurityPolicy
    from agent_swarm.security.policy import PolicyDecision

    class _StubClient:
        async def list_tools(self):
            return [
                {"name": "create_issue", "description": "x", "inputSchema": {}},
                {"name": "list_repos", "description": "y", "inputSchema": {}},
            ]
        def is_connected(self): return True
        async def connect(self): pass

    class _CountPolicy(SecurityPolicy):
        def __init__(self) -> None:
            self.calls: list[str] = []
        def check_tool(self, tool_name, arguments):  # type: ignore[override]
            self.calls.append(tool_name)
            return PolicyDecision("ALLOW", "ok")

    pol = _CountPolicy()
    adapters = await build_tool_adapters(
        "github", MCPServerConfig(name="github", transport="stdio",
                                   command=["x"]),  # type: ignore[arg-type]
        _StubClient(),  # type: ignore[arg-type]
        risk_overrides={"create_issue": "high"},
        policy=pol,
    )
    # 高风险的 create_issue 会在风险闸门被拦
    issue = next(a for a in adapters if a.mcp_tool_name == "create_issue")
    out = await issue.invoke({})
    assert "requires approval" in out
    assert pol.calls == ["mcp.github.create_issue"]  # policy 被调过
    # 低风险/未覆盖 risk 的 list_repos 走 MCP
    repos = next(a for a in adapters if a.mcp_tool_name == "list_repos")
    assert repos.policy is pol


@pytest.mark.asyncio
async def test_policy_check_tool_called_with_full_mcp_name() -> None:
    """断言 policy.check_tool 收到的是 mcp.{server}.{tool} 完整名（非裸名）"""
    from agent_swarm.security import SecurityPolicy
    from agent_swarm.security.policy import PolicyDecision

    seen: list[str] = []

    class _SpyPolicy(SecurityPolicy):
        def check_tool(self, tool_name, arguments):  # type: ignore[override]
            seen.append(tool_name)
            return PolicyDecision("ALLOW", "ok")

    client, _ = _stub_client_with_call_log()
    a = MCPToolAdapter(
        server_name="fs", mcp_tool_name="read_file",
        description="x", parameters={"type": "object"},
        client=client, risk="low", policy=_SpyPolicy(),
    )
    await a.invoke({"path": "/tmp/x"})
    assert seen == ["mcp.fs.read_file"], f"policy 收到的 tool_name 错: {seen}"
