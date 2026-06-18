"""单元测试：StdioMCPClient JSON-RPC 2.0 协议（W9-2）"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from agent_swarm.mcp.client import (
    MCPConnectionError,
    MCPRPCError,
    MCPTimeoutError,
    StdioMCPClient,
)
from agent_swarm.mcp.registry import MCPServerConfig


# ---------------------------------------------------------------------------
# 假 MCP server 脚本——通过 stdin/stdout 与客户端对话
# ---------------------------------------------------------------------------

# 协议：
# 1. 收到 initialize → 返回 capabilities + serverInfo
# 2. 收到 tools/list → 返回 1 个工具
# 3. 收到 tools/call name=echo → 返回 echo 的 arguments.content
# 4. 收到 tools/call name=boom → 返回 JSON-RPC error
# 5. 收到 tools/call name=slow → sleep 2s 再返回（用于测超时）
_FAKE_MCP_SCRIPT = r"""
import sys, json, time

def handle(req):
    method = req.get("method")
    req_id = req.get("id")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": req_id, "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "fake-mcp", "version": "0.0.1"},
        }}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": [{
            "name": "echo",
            "description": "echo back content",
            "inputSchema": {"type": "object", "properties": {"content": {"type": "string"}}},
        }, {
            "name": "boom",
            "description": "always errors",
            "inputSchema": {"type": "object"},
        }, {
            "name": "slow",
            "description": "slow tool for timeout test",
            "inputSchema": {"type": "object"},
        }]}}
    if method == "tools/call":
        name = req.get("params", {}).get("name")
        args = req.get("params", {}).get("arguments", {})
        if name == "echo":
            return {"jsonrpc": "2.0", "id": req_id, "result": {
                "content": [{"type": "text", "text": args.get("content", "")}],
            }}
        if name == "boom":
            return {"jsonrpc": "2.0", "id": req_id, "error": {
                "code": -32603, "message": "boom", "data": None,
            }}
        if name == "slow":
            time.sleep(2.0)
            return {"jsonrpc": "2.0", "id": req_id, "result": {"content": "ok"}}
    return {"jsonrpc": "2.0", "id": req_id, "error": {
        "code": -32601, "message": f"method {method} not found"
    }}

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        req = json.loads(line)
        resp = handle(req)
        print(json.dumps(resp), flush=True)
    except Exception as e:
        print(json.dumps({"jsonrpc": "2.0", "id": None, "error": {
            "code": -32700, "message": str(e)
        }}), flush=True)
"""


@pytest.fixture
def fake_mcp_script(tmp_path: Path) -> Path:
    p = tmp_path / "fake_mcp.py"
    p.write_text(_FAKE_MCP_SCRIPT, encoding="utf-8")
    return p


def _config_for(script: Path) -> MCPServerConfig:
    # timeout_s 是 StdioMCPClient 的参数，不是 MCPServerConfig 字段
    return MCPServerConfig(
        name="fake", transport="stdio",
        command=[sys.executable, str(script)],
    )


def _client_for(script: Path, timeout_s: float = 5.0) -> StdioMCPClient:
    return StdioMCPClient(_config_for(script), timeout_s=timeout_s)


# ---------------------------------------------------------------------------
# 生命周期
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connect_disconnect(fake_mcp_script: Path) -> None:
    client = _client_for(fake_mcp_script)
    assert not client.is_connected()
    await client.connect()
    assert client.is_connected()
    await client.disconnect()
    assert not client.is_connected()


@pytest.mark.asyncio
async def test_connect_with_missing_command_raises() -> None:
    cfg = MCPServerConfig(
        name="missing", transport="stdio",
        command=["definitely-not-a-real-binary-12345"],
    )
    client = StdioMCPClient(cfg)
    with pytest.raises(MCPConnectionError, match="command not found"):
        await client.connect()


def test_stdio_client_rejects_sse_transport() -> None:
    cfg = MCPServerConfig(name="x", transport="sse", url="https://x")
    with pytest.raises(ValueError, match="requires transport=stdio"):
        StdioMCPClient(cfg)


# ---------------------------------------------------------------------------
# 协议：initialize / list_tools / call_tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_initialize_handshake(fake_mcp_script: Path) -> None:
    client = _client_for(fake_mcp_script)
    result = await client.initialize()
    assert result["protocolVersion"] == "2024-11-05"
    assert result["serverInfo"]["name"] == "fake-mcp"
    await client.disconnect()


@pytest.mark.asyncio
async def test_list_tools(fake_mcp_script: Path) -> None:
    client = _client_for(fake_mcp_script)
    tools = await client.list_tools()
    names = {t["name"] for t in tools}
    assert names == {"echo", "boom", "slow"}
    await client.disconnect()


@pytest.mark.asyncio
async def test_call_tool_echo(fake_mcp_script: Path) -> None:
    client = _client_for(fake_mcp_script)
    content = await client.call_tool("echo", {"content": "hello"})
    assert content == [{"type": "text", "text": "hello"}]
    await client.disconnect()


@pytest.mark.asyncio
async def test_call_tool_rpc_error(fake_mcp_script: Path) -> None:
    client = _client_for(fake_mcp_script)
    with pytest.raises(MCPRPCError) as exc_info:
        await client.call_tool("boom", {})
    assert exc_info.value.code == -32603
    assert "boom" in exc_info.value.message
    await client.disconnect()


@pytest.mark.asyncio
async def test_call_tool_method_not_found(fake_mcp_script: Path) -> None:
    client = _client_for(fake_mcp_script)
    with pytest.raises(MCPRPCError) as exc_info:
        await client.call_tool("not_in_list", {})
    assert exc_info.value.code == -32601
    await client.disconnect()


# ---------------------------------------------------------------------------
# 超时 + 串行化
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_call_tool_timeout(fake_mcp_script: Path) -> None:
    cfg = _config_for(fake_mcp_script)
    # 缩短 timeout 让 slow tool 必然超时
    object.__setattr__(cfg, "_timeout_s_proxy", None)  # 占位避免 lint
    client = StdioMCPClient(cfg, timeout_s=0.3)
    with pytest.raises(MCPTimeoutError, match="timeout"):
        await client.call_tool("slow", {})
    await client.disconnect()


@pytest.mark.asyncio
async def test_serial_request_response(fake_mcp_script: Path) -> None:
    """W9-2 简化：in-flight 请求串行化；并发调用应按串行顺序响应"""
    client = _client_for(fake_mcp_script)
    # 串行发 3 个 echo——id 应该 1, 2, 3
    r1 = await client.call_tool("echo", {"content": "one"})
    r2 = await client.call_tool("echo", {"content": "two"})
    r3 = await client.call_tool("echo", {"content": "three"})
    assert r1[0]["text"] == "one"
    assert r2[0]["text"] == "two"
    assert r3[0]["text"] == "three"
    await client.disconnect()
