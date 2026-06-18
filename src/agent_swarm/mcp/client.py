"""
@module agent_swarm.mcp.client
@brief  W9-2 MCPClient stdio 传输——DESIGN §7.3

JSON-RPC 2.0 简化版（请求-响应）：
- 不支持通知（无 id 的消息）
- 不支持批量请求
- 不支持 Server→Client 请求（典型 MCP 模式只需要 Client→Server）

@note W9-2 仅 stdio 传输；SSE 在 W9-4 落地
@note MCP 协议层由 W9-3 适配器（MCPToolAdapter）包装；本类只暴露底层
      list_tools / call_tool 方法
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from agent_swarm.mcp.registry import MCPServerConfig

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 错误类型
# ---------------------------------------------------------------------------


class MCPError(Exception):
    """MCP 协议层错误基类"""


class MCPRPCError(MCPError):
    """JSON-RPC 错误响应（code + message + data）"""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        self.code = code
        self.message = message
        self.data = data
        super().__init__(f"MCP RPC error {code}: {message}")


class MCPTimeoutError(MCPError):
    """MCP 调用超时"""


class MCPConnectionError(MCPError):
    """MCP 子进程启动失败 / 早期断开"""


# ---------------------------------------------------------------------------
# MCPClient ABC
# ---------------------------------------------------------------------------


class MCPClient(ABC):
    """MCP 客户端抽象基类——stdio / sse 都继承"""

    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def disconnect(self) -> None: ...

    @abstractmethod
    async def list_tools(self) -> list[dict[str, Any]]: ...

    @abstractmethod
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any: ...

    @abstractmethod
    def is_connected(self) -> bool: ...


# ---------------------------------------------------------------------------
# stdio 传输
# ---------------------------------------------------------------------------


@dataclass
class _PendingRequest:
    """in-flight 请求：future + timeout 任务"""
    future: asyncio.Future
    timeout_handle: asyncio.Handle


class StdioMCPClient(MCPClient):
    """
    stdio 传输 MCP 客户端——DESIGN §7.3

    启动方式：subprocess.Popen(command, env=env, cwd=cwd, stdin=PIPE, stdout=PIPE)
    协议：JSON-RPC 2.0 over stdio（每行一个 JSON 对象）
    并发：asyncio.Lock 保护 stdin 写入；后台 task 持续读 stdout 派发响应

    @note W9-2 简化版：单 in-flight 请求串行化（一个 future 完成再发下一个）
          简单可靠；MCP 工具调用通常慢，串行化吞吐也够
    @note MCP 进程日志走 stderr；通过 returncode / poll 监测存活
    """

    # JSON-RPC 2.0 标准错误码（DESIGN §7.3 + JSON-RPC spec）
    PARSE_ERROR = -32700
    INVALID_REQUEST = -32600
    METHOD_NOT_FOUND = -32601
    INVALID_PARAMS = -32602
    INTERNAL_ERROR = -32693

    def __init__(
        self,
        config: MCPServerConfig,
        timeout_s: float = 30.0,
    ) -> None:
        if config.transport != "stdio":
            raise ValueError(
                f"StdioMCPClient requires transport=stdio, got {config.transport!r}"
            )
        self._config = config
        self._timeout_s = timeout_s
        self._process: asyncio.subprocess.Process | None = None
        self._stdin_lock = asyncio.Lock()
        self._next_id = 1
        self._reader_task: asyncio.Task | None = None
        # in-flight 请求字典：req_id -> Future；_read_loop 派发响应到这里
        self._pending: dict[int, asyncio.Future] = {}

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def connect(self) -> None:
        """启动子进程 + 启动后台 reader task + stderr drain

        H3 fix：重复调用 connect() 时清空旧 _pending（防内存泄漏 +
        旧 future 误命中新 id）；重置 _next_id=1 让 id 序列干净。
        """
        if self.is_connected():
            return
        # H3: 防 _pending 状态泄漏
        for f in self._pending.values():
            if not f.done():
                f.set_exception(MCPConnectionError(
                    f"MCP {self._config.name} reconnecting; old request cancelled"
                ))
        self._pending.clear()
        self._next_id = 1
        # W9-2 简化：env 透传 config.env（不展开 SecretManager——DESIGN §7.3
        # "凭证管理" 留给 SecretManager 集成，本类只接 dict 形式 env）
        full_env = dict(self._config.env) if self._config.env else None
        try:
            self._process = await asyncio.create_subprocess_exec(
                *self._config.command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self._config.cwd,
                env=full_env,
            )
        except FileNotFoundError as exc:
            raise MCPConnectionError(
                f"MCP server {self._config.name!r} command not found: {exc}"
            ) from exc
        except OSError as exc:
            raise MCPConnectionError(
                f"MCP server {self._config.name!r} failed to start: {exc}"
            ) from exc

        self._reader_task = asyncio.create_task(
            self._read_loop(), name=f"mcp-stdio-reader-{self._config.name}",
        )
        # M1: 并行 stderr drain——server stderr 输出（npm 启动日志等）
        # 累积到 OS pipe buffer 满后阻塞 server，进而阻塞 stdout readline；
        # 持续 drain 让 stderr 不会撑爆 buffer
        self._stderr_task = asyncio.create_task(
            self._stderr_drain_loop(),
            name=f"mcp-stdio-stderr-{self._config.name}",
        )
        log.info("MCP stdio client connected: %s (pid=%d)",
                 self._config.name, self._process.pid)

    async def disconnect(self) -> None:
        """关闭 reader + stderr drain + 终止子进程"""
        for task_attr in ("_reader_task", "_stderr_task"):
            t = getattr(self, task_attr, None)
            if t is not None:
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
                setattr(self, task_attr, None)
        if self._process is not None:
            try:
                self._process.terminate()
                await asyncio.wait_for(self._process.wait(), timeout=2.0)
            except (asyncio.TimeoutError, ProcessLookupError):
                try:
                    self._process.kill()
                except ProcessLookupError:
                    pass
            self._process = None
        log.info("MCP stdio client disconnected: %s", self._config.name)

    def is_connected(self) -> bool:
        if self._process is None:
            return False
        return self._process.returncode is None

    # ------------------------------------------------------------------
    # JSON-RPC 请求
    # ------------------------------------------------------------------
    async def _request(
        self, method: str, params: dict[str, Any] | None = None,
    ) -> Any:
        """
        发送 JSON-RPC 请求并等待响应

        @param method JSON-RPC method 名（如 "tools/list", "tools/call"）
        @param params JSON-RPC params
        @return response["result"]
        @raise MCPRPCError response 含 error
        @raise MCPTimeoutError 超时
        @raise MCPConnectionError 子进程断开
        """
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

        # 串行化：lock + future
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[req_id] = future

        try:
            async with self._stdin_lock:
                if self._process is None or self._process.stdin is None:
                    raise MCPConnectionError("process stdin closed")
                line = json.dumps(request) + "\n"
                self._process.stdin.write(line.encode("utf-8"))
                await self._process.stdin.drain()

            # 等响应（带超时）
            try:
                return await asyncio.wait_for(future, timeout=self._timeout_s)
            except asyncio.TimeoutError as exc:
                raise MCPTimeoutError(
                    f"MCP {method} (id={req_id}) timeout after {self._timeout_s}s"
                ) from exc
        finally:
            self._pending.pop(req_id, None)

    # ------------------------------------------------------------------
    # MCP 协议层（DESIGN §7.3）
    # ------------------------------------------------------------------
    async def initialize(self) -> dict[str, Any]:
        """MCP 协议握手：client → server 介绍自己 + 协议版本"""
        return await self._request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "agent-swarm", "version": "0.1.0"},
        })

    async def list_tools(self) -> list[dict[str, Any]]:
        """MCP tools/list——返回 [{"name": ..., "description": ..., "inputSchema": ...}, ...]"""
        result = await self._request("tools/list", {})
        return list(result.get("tools", []))

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """MCP tools/call——返回 content 字段（str 或 list）"""
        result = await self._request("tools/call", {
            "name": name,
            "arguments": arguments,
        })
        return result.get("content")

    # ------------------------------------------------------------------
    # 内部：reader loop + pending 字典
    # ------------------------------------------------------------------
    async def _stderr_drain_loop(self) -> None:
        """M1 fix：持续读 stderr 防 buffer 满阻塞 server

        @note stderr 内容只记 log（避免刷屏），不向上抛——MCP 协议本身只用 stdout
        """
        try:
            while self.is_connected() and self._process is not None:
                if self._process.stderr is None:
                    break
                line = await self._process.stderr.readline()
                if not line:
                    break
                log.debug(
                    "MCP %s stderr: %s",
                    self._config.name, line.decode("utf-8", errors="replace").rstrip(),
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("MCP %s stderr drain crashed: %s",
                        self._config.name, exc)

    async def _read_loop(self) -> None:
        """后台 task：持续读 stdout，按 id 派发到 pending future"""
        try:
            while self.is_connected() and self._process is not None:
                if self._process.stdout is None:
                    break
                line = await self._process.stdout.readline()
                if not line:
                    # EOF——子进程退出
                    log.warning("MCP %s stdout EOF; disconnecting",
                                self._config.name)
                    break
                try:
                    msg = json.loads(line.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    log.warning("MCP %s invalid JSON: %s",
                                self._config.name, exc)
                    continue
                if not isinstance(msg, dict):
                    continue
                req_id = msg.get("id")
                if not isinstance(req_id, int):
                    continue  # 通知/无 id 消息——W9-2 简化版忽略
                future = self._pending.pop(req_id, None)
                if future is None or future.done():
                    continue
                if "error" in msg:
                    err = msg["error"]
                    future.set_exception(MCPRPCError(
                        code=err.get("code", 0),
                        message=err.get("message", ""),
                        data=err.get("data"),
                    ))
                elif "result" in msg:
                    future.set_result(msg["result"])
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("MCP %s read_loop crashed: %s", self._config.name, exc)
        finally:
            # 把所有 pending future 标记失败
            for f in self._pending.values():
                if not f.done():
                    f.set_exception(MCPConnectionError(
                        f"MCP {self._config.name} read loop ended"
                    ))
            self._pending.clear()


__all__ = [
    "MCPClient",
    "MCPConnectionError",
    "MCPError",
    "MCPRPCError",
    "MCPTimeoutError",
    "StdioMCPClient",
]
