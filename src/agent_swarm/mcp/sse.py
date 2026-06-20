"""
@module agent_swarm.mcp.sse
@brief  W14a-3 SseMCPClient——JSON-RPC 2.0 over HTTP POST + SSE 接收——DESIGN §7.3

MCP 2024-11-05 SSE 传输协议（简化版）：
  - 客户端 → POST application/json 到 server URL
  - 服务端 → 响应 Content-Type: text/event-stream
  - 每个 SSE event: message / data: <json> = 一条 JSON-RPC 响应
  - event: endpoint / data: <url> = server 给出 GET endpoint（接收通知，
    W14a 不实现——agent-swarm 用例只需要 client→server 单向）

W14a-3 简化：
  - 单 in-flight 请求串行化（同 stdio）
  - 不支持 server→client 通知（notifications/initialized 等等）——initialize
    完成后 server 主动 push 的通知会触发 SSE event 但被忽略
  - bearer token auth（DESIGN §7.3）
  - 不实现 GET endpoint 轮询（不需要 server→client）

@note aiohttp 是项目已有依赖（DESIGN §7.3 + §10.1）——直接用 ClientSession
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any

import aiohttp

from agent_swarm.mcp.client import (
    MCPClient,
    MCPConnectionError,
    MCPRPCError,
    MCPTimeoutError,
)
from agent_swarm.mcp.registry import MCPServerConfig

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 错误
# ---------------------------------------------------------------------------


class MCPHTTPError(MCPConnectionError):
    """SSE/HTTP 传输层错误——非 2xx 响应

    @note W14a-3 加：与 MCPConnectionError 区分便于重试策略
    """


# ---------------------------------------------------------------------------
# SseMCPClient
# ---------------------------------------------------------------------------


@dataclass
class _PendingRequest:
    """in-flight 请求：future + 超时 deadline"""
    future: asyncio.Future
    deadline: float


class SseMCPClient(MCPClient):
    """
    SSE 传输 MCP 客户端——DESIGN §7.3

    协议：JSON-RPC 2.0 over HTTP POST + SSE
      - POST {url} Content-Type: application/json → 返回 text/event-stream
      - 单向：只 client→server；server 响应一条 SSE event 关闭流
      - 简化：串行化（lock + future），一次只一个 in-flight 请求

    @note bearer token 通过 Authorization header（DESIGN §7.3 auth=bearer）
    @note 简化版用 aiohttp 单次 POST 拿到 SSE 流；不维护长连接
          （每次请求独立 POST，SSE 流响应一次关闭）
    """

    def __init__(
        self,
        config: MCPServerConfig,
        timeout_s: float = 30.0,
    ) -> None:
        if config.transport != "sse":
            raise ValueError(
                f"SseMCPClient requires transport=sse, got {config.transport!r}"
            )
        if not config.url:
            raise ValueError(
                f"SseMCPClient requires non-empty url in {config.name!r}"
            )
        self._config = config
        self._timeout_s = timeout_s
        self._session: aiohttp.ClientSession | None = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future] = {}
        self._connected = False
        # W14a-3 用 semaphore 替代 lock（W14a-5 reliability 会重构成显式 lock）
        self._stdin_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def connect(self) -> None:
        """建立 aiohttp session + 做一次 initialize 握手确认连通性

        H3 fix（与 stdio 对齐）：重复 connect 时清空旧 _pending
        @note 内部 initialize 握手用 _send_request_raw（不走 _request 的
              self-connect 守卫）防无限递归
        """
        if self._connected and self._session is not None and not self._session.closed:
            return
        # H3: 防 _pending 状态泄漏
        for f in self._pending.values():
            if not f.done():
                f.set_exception(MCPConnectionError(
                    f"MCP {self._config.name} reconnecting; old request cancelled"
                ))
        self._pending.clear()
        self._next_id = 1
        # 准备 session
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self._timeout_s),
            )
        # 试探握手：发 initialize 请求，确认 SSE 通道可用
        try:
            req_id = self._next_id
            self._next_id += 1
            request = {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "agent-swarm", "version": "0.1.0"},
                },
            }
            response = await self._post_and_read_sse(request)
            if "error" in response:
                raise MCPRPCError(
                    code=response["error"].get("code", 0),
                    message=response["error"].get("message", ""),
                )
        except Exception as exc:
            await self._close_session()
            raise MCPConnectionError(
                f"MCP SSE {self._config.name!r} handshake failed: {exc}"
            ) from exc
        self._connected = True
        log.info("MCP SSE client connected: %s (url=%s)",
                 self._config.name, self._config.url)

    async def disconnect(self) -> None:
        """关闭 session；清空 pending"""
        for f in self._pending.values():
            if not f.done():
                f.set_exception(MCPConnectionError(
                    f"MCP {self._config.name} disconnected"
                ))
        self._pending.clear()
        await self._close_session()
        self._connected = False
        log.info("MCP SSE client disconnected: %s", self._config.name)

    async def _close_session(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    def is_connected(self) -> bool:
        return (
            self._connected
            and self._session is not None
            and not self._session.closed
        )

    # ------------------------------------------------------------------
    # HTTP headers
    # ------------------------------------------------------------------
    def _build_headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        if self._config.auth == "bearer" and self._config.token:
            headers["Authorization"] = f"Bearer {self._config.token}"
        return headers

    # ------------------------------------------------------------------
    # SSE 流解析
    # ------------------------------------------------------------------
    async def _post_and_read_sse(
        self, request: dict[str, Any],
    ) -> dict[str, Any]:
        """POST 一个 JSON-RPC 请求，读取 SSE 流解析出 response

        @return 解析出的 JSON-RPC 响应对象（dict）
        @raise MCPHTTPError HTTP 状态非 2xx
        @raise MCPTimeoutError 超时
        @raise MCPConnectionError 网络错误
        @raise MCPRPCError 响应含 error 字段
        """
        if self._session is None or self._session.closed:
            raise MCPConnectionError("session not connected")
        url = self._config.url or ""
        body = json.dumps(request)
        try:
            async with self._session.post(
                url, data=body, headers=self._build_headers(),
            ) as resp:
                if resp.status < 200 or resp.status >= 300:
                    text = await resp.text()
                    raise MCPHTTPError(
                        f"MCP SSE POST {url} status={resp.status}: {text[:200]}"
                    )
                # text/event-stream；按行解析 event:/data: 字段
                return await self._parse_sse_response(resp)
        except aiohttp.ClientError as exc:
            raise MCPConnectionError(
                f"MCP SSE {self._config.name!r} network error: {exc}"
            ) from exc

    async def _parse_sse_response(
        self, resp: aiohttp.ClientResponse,
    ) -> dict[str, Any]:
        """从 SSE 响应里解析出 JSON-RPC 响应对象

        SSE 格式：
            event: message
            data: {"jsonrpc": "2.0", "id": 1, "result": ...}

            <blank line>
        W14a 简化：只取 data 行里第一个 JSON；忽略 event: endpoint 等
        """
        event_type: str | None = None
        data_lines: list[str] = []

        async for raw_line in resp.content:
            try:
                line = raw_line.decode("utf-8").rstrip("\r\n")
            except UnicodeDecodeError:
                continue
            if not line:
                # 事件结束——如有累积 data 就处理
                if data_lines and event_type in (None, "message"):
                    payload = "\n".join(data_lines)
                    try:
                        msg = json.loads(payload)
                    except json.JSONDecodeError as exc:
                        raise MCPRPCError(
                            code=-32700,
                            message=f"SSE invalid JSON: {exc}",
                        ) from exc
                    if isinstance(msg, dict):
                        return msg
                event_type = None
                data_lines = []
                continue
            if line.startswith(":"):
                continue  # SSE comment
            if ":" in line:
                field, _, value = line.partition(":")
                # SSE 规范：冒号后可选单空格
                if value.startswith(" "):
                    value = value[1:]
                if field == "event":
                    event_type = value
                elif field == "data":
                    data_lines.append(value)
                # 其他字段（id / retry）忽略
        # 流末尾如还有 data
        if data_lines and event_type in (None, "message"):
            payload = "\n".join(data_lines)
            try:
                msg = json.loads(payload)
            except json.JSONDecodeError as exc:
                raise MCPRPCError(
                    code=-32700,
                    message=f"SSE invalid JSON: {exc}",
                ) from exc
            if isinstance(msg, dict):
                return msg
        raise MCPConnectionError(
            f"MCP SSE {self._config.name!r} stream ended without JSON-RPC response"
        )

    # ------------------------------------------------------------------
    # JSON-RPC 请求（与 stdio 协议一致）
    # ------------------------------------------------------------------
    async def _request(
        self, method: str, params: dict[str, Any] | None = None,
    ) -> Any:
        if not self.is_connected():
            await self.connect()

        req_id = self._next_id
        self._next_id += 1
        request = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
        }
        if params is not None:
            request["params"] = params

        try:
            async with self._stdin_lock:  # 复用 lock 字段名（含义=串行化）
                response = await self._post_and_read_sse(request)
        except MCPTimeoutError:
            raise
        except MCPHTTPError:
            raise
        except MCPRPCError:
            raise
        except MCPConnectionError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise MCPConnectionError(
                f"MCP SSE {self._config.name!r} unexpected: {exc}"
            ) from exc

        if "error" in response:
            err = response["error"]
            raise MCPRPCError(
                code=err.get("code", 0),
                message=err.get("message", ""),
                data=err.get("data"),
            )
        return response.get("result")

    # ------------------------------------------------------------------
    # MCP 协议层（与 StdioMCPClient 一致）
    # ------------------------------------------------------------------
    async def initialize(self) -> dict[str, Any]:
        return await self._request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "agent-swarm", "version": "0.1.0"},
        })

    async def list_tools(self) -> list[dict[str, Any]]:
        result = await self._request("tools/list", {})
        return list(result.get("tools", []))

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        result = await self._request("tools/call", {
            "name": name,
            "arguments": arguments,
        })
        return result.get("content")


__all__ = [
    "MCPHTTPError",
    "SseMCPClient",
]
