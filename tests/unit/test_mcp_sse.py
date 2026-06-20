"""
@module tests.unit.test_mcp_sse
@brief  W14a-3 SseMCPClient 单元测试——DESIGN §7.3 SSE 传输

覆盖：
  - 构造校验：transport=sse / url 必填
  - 握手失败抛 MCPConnectionError
  - SSE 流解析：event: message + data: {...} → JSON-RPC response
  - 忽略 event: endpoint 等非 message 事件
  - HTTP 非 2xx → MCPHTTPError
  - bearer token auth → Authorization header
  - list_tools / call_tool 通过 _request 走通
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from agent_swarm.mcp.client import (
    MCPConnectionError,
    MCPRPCError,
)
from agent_swarm.mcp.registry import MCPServerConfig
from agent_swarm.mcp.sse import MCPHTTPError, SseMCPClient


def _make_sse_response(events: list[str], status: int = 200):
    """构造可作 aiohttp 上下文管理器使用的 mock response"""
    body = "\n\n".join(events) + "\n\n"
    raw_lines = [line.encode("utf-8") for line in body.splitlines(keepends=True)]

    class _CM:
        """最小 aiohttp 兼容上下文管理器——只支持 .status/.content/.text + __aenter__/__aexit__"""

        def __init__(self):
            self.status = status
            self._iter = iter(raw_lines)
            self._text = "error body" if status >= 400 else ""

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        @property
        def content(self):
            return self

        async def __aiter__(self):
            for line in self._iter:
                yield line

        async def text(self):
            return self._text

    # 兼容 aiohttp 接口：resp.text() 是 coroutine
    return _CM()


def _make_mock_session(responses: list):
    """构造 mock aiohttp.ClientSession：post() 同步返回 _CM（已构造好的 context manager）"""
    responses = list(responses)  # copy

    class _MockSession:
        closed = False

        def post(self, *args, **kwargs):
            # aiohttp.ClientSession.post() 是 sync，返回 _RequestContextManager（可用 async with）
            return responses.pop(0) if responses else _make_sse_response([], status=200)

        async def close(self):
            self.closed = True

    return _MockSession()


def _sse_event(data: str, event_type: str = "message") -> str:
    return f"event: {event_type}\ndata: {data}"


def _make_config(**overrides) -> MCPServerConfig:
    defaults = dict(
        name="sse-test",
        transport="sse",
        url="http://localhost:8765/mcp",
        auth="none",
    )
    defaults.update(overrides)
    return MCPServerConfig(**defaults)


def test_construct_requires_sse_transport() -> None:
    cfg = MCPServerConfig(name="x", transport="stdio", command=["x"])
    with pytest.raises(ValueError, match="transport=sse"):
        SseMCPClient(cfg)


def test_construct_requires_url() -> None:
    """MCPServerConfig 本身就拒空 url（sse 必须）"""
    with pytest.raises(ValueError, match="non-empty 'url'"):
        MCPServerConfig(name="x", transport="sse", url="")


def test_construct_bearer_requires_token() -> None:
    """bearer auth 但缺 token 时 MCPServerConfig.__post_init__ 已拒"""
    with pytest.raises(ValueError, match="auth=bearer requires 'token'"):
        MCPServerConfig(
            name="x", transport="sse", url="http://x", auth="bearer",
        )


def test_build_headers_no_auth() -> None:
    cfg = _make_config()
    client = SseMCPClient(cfg)
    headers = client._build_headers()
    assert headers["Content-Type"] == "application/json"
    assert headers["Accept"] == "text/event-stream"
    assert "Authorization" not in headers


def test_build_headers_bearer() -> None:
    cfg = _make_config(auth="bearer", token="secret-token-123")
    client = SseMCPClient(cfg)
    headers = client._build_headers()
    assert headers["Authorization"] == "Bearer secret-token-123"


@pytest.mark.asyncio
async def test_connect_handshake_failure_raises() -> None:
    """connect() 内含 initialize 握手——失败抛 MCPConnectionError"""
    cfg = _make_config()
    client = SseMCPClient(cfg)

    class _MockSession:
        closed = False

        def post(self, *args, **kwargs):
            raise OSError("connection refused")

        async def close(self):
            pass

    with (
        patch.object(client, "_session", _MockSession()),
        pytest.raises(MCPConnectionError, match="handshake failed"),
    ):
        await client.connect()


@pytest.mark.asyncio
async def test_sse_parse_message_event_returns_dict() -> None:
    """SSE 流解析：event: message + data: {...} → dict"""
    cfg = _make_config()
    client = SseMCPClient(cfg)

    resp = _make_sse_response([
        _sse_event('{"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}'),
    ])

    parsed = await client._parse_sse_response(resp)
    assert parsed["id"] == 1
    assert parsed["result"] == {"tools": []}


@pytest.mark.asyncio
async def test_sse_parse_ignores_non_message_events() -> None:
    """非 message event 跳过；只取 message 的 data"""
    cfg = _make_config()
    client = SseMCPClient(cfg)

    resp = _make_sse_response([
        _sse_event("http://localhost:8765/sse", event_type="endpoint"),
        _sse_event('{"jsonrpc": "2.0", "id": 1, "result": {"ok": true}}'),
    ])

    parsed = await client._parse_sse_response(resp)
    assert parsed["id"] == 1
    assert parsed["result"] == {"ok": True}


@pytest.mark.asyncio
async def test_sse_parse_multiline_data() -> None:
    """data 字段可多行（\\n 分隔），JSON 解析时拼回"""
    cfg = _make_config()
    client = SseMCPClient(cfg)

    # 单个 event 内多行 data；按 SSE 规范空行结束
    single_event = "event: message\ndata: {\"jsonrpc\": \"2.0\",\ndata: \"id\": 1,\ndata: \"result\": {}}"
    resp = _make_sse_response([single_event])

    parsed = await client._parse_sse_response(resp)
    assert parsed["id"] == 1
    assert parsed["result"] == {}


@pytest.mark.asyncio
async def test_sse_parse_invalid_json_raises() -> None:
    """data 不是合法 JSON → MCPRPCError (parse error)"""
    cfg = _make_config()
    client = SseMCPClient(cfg)

    resp = _make_sse_response([
        _sse_event("not json {{"),
    ])

    with pytest.raises(MCPRPCError, match="SSE invalid JSON"):
        await client._parse_sse_response(resp)


@pytest.mark.asyncio
async def test_sse_parse_empty_stream_raises() -> None:
    """SSE 流没有任何 message event → MCPConnectionError"""
    cfg = _make_config()
    client = SseMCPClient(cfg)

    resp = _make_sse_response([
        _sse_event("http://x", event_type="endpoint"),
    ])

    with pytest.raises(MCPConnectionError, match="without JSON-RPC response"):
        await client._parse_sse_response(resp)


@pytest.mark.asyncio
async def test_sse_parse_handles_comment_lines() -> None:
    """SSE 注释行（:开头）应跳过"""
    cfg = _make_config()
    client = SseMCPClient(cfg)

    resp = _make_sse_response([
        ": this is a comment",
        _sse_event('{"jsonrpc": "2.0", "id": 5, "result": "x"}'),
    ])

    parsed = await client._parse_sse_response(resp)
    assert parsed["id"] == 5


@pytest.mark.asyncio
async def test_post_non_2xx_raises_http_error() -> None:
    """HTTP 状态 4xx/5xx → MCPHTTPError"""
    cfg = _make_config()
    client = SseMCPClient(cfg)

    resp = _make_sse_response([], status=500)
    with (
        patch.object(client, "_session", _make_mock_session([resp])),
        pytest.raises(MCPHTTPError, match="status=500"),
    ):
        await client._post_and_read_sse({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})


@pytest.mark.asyncio
async def test_list_tools_returns_tools_list() -> None:
    """list_tools 走通 tools/list 请求"""
    cfg = _make_config()
    client = SseMCPClient(cfg)

    init_resp = _make_sse_response([
        _sse_event('{"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2024-11-05"}}'),
    ])
    list_resp = _make_sse_response([
        _sse_event('{"jsonrpc": "2.0", "id": 2, "result": {"tools": [{"name": "echo"}]}}'),
    ])

    with patch.object(client, "_session", _make_mock_session([init_resp, list_resp])):
        await client.connect()
        tools = await client.list_tools()

    assert len(tools) == 1
    assert tools[0]["name"] == "echo"


@pytest.mark.asyncio
async def test_call_tool_returns_content() -> None:
    cfg = _make_config()
    client = SseMCPClient(cfg)

    init_resp = _make_sse_response([
        _sse_event('{"jsonrpc": "2.0", "id": 1, "result": {}}'),
    ])
    call_resp = _make_sse_response([
        _sse_event('{"jsonrpc": "2.0", "id": 2, "result": {"content": [{"type": "text", "text": "hi"}]}}'),
    ])

    with patch.object(client, "_session", _make_mock_session([init_resp, call_resp])):
        await client.connect()
        result = await client.call_tool("echo", {"x": 1})

    assert result == [{"type": "text", "text": "hi"}]


@pytest.mark.asyncio
async def test_call_tool_rpc_error_raises() -> None:
    """JSON-RPC 响应含 error 字段 → MCPRPCError"""
    cfg = _make_config()
    client = SseMCPClient(cfg)

    init_resp = _make_sse_response([
        _sse_event('{"jsonrpc": "2.0", "id": 1, "result": {}}'),
    ])
    call_resp = _make_sse_response([
        _sse_event('{"jsonrpc": "2.0", "id": 2, "error": {"code": -32601, "message": "Method not found"}}'),
    ])

    with patch.object(client, "_session", _make_mock_session([init_resp, call_resp])):
        await client.connect()
        with pytest.raises(MCPRPCError, match="Method not found"):
            await client.call_tool("missing", {})


@pytest.mark.asyncio
async def test_disconnect_clears_state() -> None:
    cfg = _make_config()
    client = SseMCPClient(cfg)
    client._connected = True
    client._next_id = 42

    await client.disconnect()
    assert client.is_connected() is False
    # disconnect 不重置 _next_id（connect() 在 reconnect 路径才会重置）
    assert client._next_id == 42
