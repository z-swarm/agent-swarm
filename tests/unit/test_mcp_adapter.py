"""单元测试：MCPToolAdapter（W9-3）"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from agent_swarm.mcp.adapter import (
    MCPToolAdapter,
    _serialize_content,
    await_build_tool_adapters,
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
    client = StdioMCPClient(cfg, timeout_s=5.0)
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
