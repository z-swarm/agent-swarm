"""
@module agent_swarm.mcp.registry
@brief  W9-1/W14a-4 MCPRegistry + MCPServerConfig + 连接管理——DESIGN §7.3

W9 范围：先落地注册表 + 配置数据类，客户端/适配器 W9-2/W9-3 接入。
W14a-4 扩展：连接管理（connect_all / disconnect_all / health_check）——DESIGN §7.3
            "连接监控：MCPRegistry 后台任务持续监测 server 健康"
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from agent_swarm.mcp.client import MCPClient
    from agent_swarm.mcp.reliability import ReconnectingMCPClient

log = logging.getLogger(__name__)

# MCP 传输类型（DESIGN §7.3 配置示例）
Transport = Literal["stdio", "sse"]


@dataclass
class MCPServerConfig:
    """
    MCP 服务器配置——DESIGN §7.3

    字段对应 YAML 配置：
      stdio: command (list[str]) + env (dict) + cwd (str, optional)
      sse:   url (str) + auth (Literal["bearer", "none"], optional) + token (str, optional)

    @note token 强制走 SecretManager（DESIGN §7.3 "凭证管理"）——本类只承载
          token 引用（如 "${MCP_DB_TOKEN}"），不存明文
    """

    name: str
    transport: Transport
    # stdio 字段
    command: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    # sse 字段
    url: str | None = None
    auth: Literal["bearer", "none"] = "none"
    token: str | None = None
    # 可靠性配置
    auto_reconnect: bool = True
    max_reconnect_attempts: int = 5
    circuit_breaker_threshold: int = 3
    # 工具风险覆写（DESIGN §7.3 risk_overrides：create_issue: high）
    risk_overrides: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """配置校验：transport 决定必填字段"""
        if not self.name or not self.name.strip():
            raise ValueError("MCPServerConfig.name must be non-empty")
        if self.transport == "stdio":
            if not self.command:
                raise ValueError(
                    f"MCPServerConfig[{self.name!r}] transport=stdio requires "
                    f"non-empty 'command'"
                )
            if self.url is not None:
                raise ValueError(
                    f"MCPServerConfig[{self.name!r}] transport=stdio should not "
                    f"set 'url' (sse-only field)"
                )
        elif self.transport == "sse":
            if not self.url:
                raise ValueError(
                    f"MCPServerConfig[{self.name!r}] transport=sse requires "
                    f"non-empty 'url'"
                )
            if self.command:
                raise ValueError(
                    f"MCPServerConfig[{self.name!r}] transport=sse should not "
                    f"set 'command' (stdio-only field)"
                )
            if self.auth == "bearer" and not self.token:
                raise ValueError(
                    f"MCPServerConfig[{self.name!r}] auth=bearer requires 'token'"
                )
        else:
            raise ValueError(
                f"MCPServerConfig[{self.name!r}] transport must be stdio or sse, "
                f"got {self.transport!r}"
            )


@dataclass
class MCPHealthStatus:
    """单个 server 的健康状态——DESIGN §7.3 "连接监控" 暴露"""
    name: str
    connected: bool
    circuit_state: str  # "closed" / "open" / "half_open"
    consecutive_failures: int
    last_check_at: float = 0.0
    last_error: str | None = None


class MCPRegistry:
    """
    MCP 服务器注册表——DESIGN §7.3

    W9-1 范围：管理 server 配置（register / get / list / remove）
    W9-2+ 扩展：连接管理（connect / disconnect / list_tools / shutdown）
    W14a-4 扩展：connect_all / disconnect_all / health_check_all

    @note W14a-4: connect_all() 创建 ReconnectingMCPClient 包装的 client
          （自动重连 + 熔断），存到 _clients dict
    """

    def __init__(self) -> None:
        self._servers: dict[str, MCPServerConfig] = {}
        # W14a-4: client 管理（ReconnectingMCPClient 包装的）
        self._clients: dict[str, ReconnectingMCPClient] = {}
        self._lock = asyncio.Lock()

    def register(self, config: MCPServerConfig) -> None:
        if config.name in self._servers:
            raise ValueError(
                f"MCP server {config.name!r} already registered"
            )
        self._servers[config.name] = config

    def get(self, name: str) -> MCPServerConfig:
        if name not in self._servers:
            raise KeyError(f"MCP server {name!r} not registered")
        return self._servers[name]

    def try_get(self, name: str) -> MCPServerConfig | None:
        return self._servers.get(name)

    def list_names(self) -> list[str]:
        return list(self._servers.keys())

    def list_all(self) -> list[MCPServerConfig]:
        return list(self._servers.values())

    def remove(self, name: str) -> bool:
        return self._servers.pop(name, None) is not None

    def __len__(self) -> int:
        return len(self._servers)

    def __contains__(self, name: str) -> bool:
        return name in self._servers

    # ------------------------------------------------------------------
    # W14a-4: 连接管理 + 健康检查
    # ------------------------------------------------------------------
    def get_client(self, name: str) -> ReconnectingMCPClient | None:
        """取已连接的 client（ReconnectingMCPClient 包装）；未连接返回 None"""
        return self._clients.get(name)

    def list_clients(self) -> dict[str, ReconnectingMCPClient]:
        return dict(self._clients)

    async def connect_all(self) -> dict[str, bool]:
        """
        并发 connect 所有 server；返回 {name: success}

        @note 单个 server 连接失败不影响其他——DESIGN §7.3 "连接监控"
        @note 失败时 client 不存进 _clients，调用方 health_check 可见
        """
        results: dict[str, bool] = {}
        if not self._servers:
            return results
        async with self._lock:
            tasks = {
                name: asyncio.create_task(
                    self._connect_one(name, cfg),
                    name=f"mcp-connect-{name}",
                )
                for name, cfg in self._servers.items()
            }
            for name, t in tasks.items():
                try:
                    results[name] = await t
                except Exception as exc:  # noqa: BLE001
                    log.warning("MCP %s connect failed: %s", name, exc)
                    results[name] = False
        return results

    async def _connect_one(
        self, name: str, cfg: MCPServerConfig,
    ) -> bool:
        """连接单个 server 并包成 ReconnectingMCPClient 存进 _clients"""
        # 避免循环 import
        from agent_swarm.mcp.client import StdioMCPClient
        from agent_swarm.mcp.reliability import ReconnectingMCPClient
        from agent_swarm.mcp.sse import SseMCPClient

        if cfg.transport == "stdio":
            inner: MCPClient = StdioMCPClient(cfg)
        elif cfg.transport == "sse":
            inner = SseMCPClient(cfg)
        else:
            raise ValueError(f"unsupported transport: {cfg.transport!r}")

        wrapper = ReconnectingMCPClient(inner, cfg)
        try:
            await wrapper.connect()
        except Exception as exc:  # noqa: BLE001
            log.warning("MCP %s connect failed: %s", name, exc)
            try:
                await wrapper.disconnect()
            except Exception:  # noqa: BLE001
                pass
            return False
        self._clients[name] = wrapper
        log.info("MCP %s connected (reliability wrapper ready)", name)
        return True

    async def disconnect_all(self) -> None:
        """断开所有 client"""
        async with self._lock:
            clients = list(self._clients.values())
            self._clients.clear()
        for c in clients:
            try:
                await c.disconnect()
            except Exception as exc:  # noqa: BLE001
                log.warning("MCP %s disconnect error: %s", c.inner, exc)

    async def health_check(self, name: str) -> MCPHealthStatus:
        """
        单个 server 健康检查（DESIGN §7.3 "连接监控"）

        @return MCPHealthStatus（不抛错——失败时 last_error 填值）
        """
        import time as _time

        cfg = self._servers.get(name)
        if cfg is None:
            return MCPHealthStatus(
                name=name, connected=False, circuit_state="closed",
                consecutive_failures=0, last_check_at=_time.time(),
                last_error=f"server {name!r} not registered",
            )
        client = self._clients.get(name)
        if client is None:
            return MCPHealthStatus(
                name=name, connected=False, circuit_state="closed",
                consecutive_failures=0, last_check_at=_time.time(),
                last_error="client not initialized (call connect_all first)",
            )
        connected = client.is_connected()
        cb = client.circuit_breaker
        return MCPHealthStatus(
            name=name,
            connected=connected,
            circuit_state=cb.state,
            consecutive_failures=cb.consecutive_failures,
            last_check_at=_time.time(),
        )

    async def health_check_all(self) -> list[MCPHealthStatus]:
        """并发 health_check 所有 server"""
        if not self._servers:
            return []
        return await asyncio.gather(*(self.health_check(n) for n in self._servers))

    # ------------------------------------------------------------------
    # 工厂：from_dict / from_yaml——DESIGN §7.3 YAML 配置示例的解析
    # ------------------------------------------------------------------
    @classmethod
    def from_dict(cls, cfg: dict[str, Any]) -> MCPRegistry:
        """
        从 dict 构造 registry

        @param cfg 形如 {"github": {"transport": "stdio", "command": [...], ...}, ...}
        """
        registry = cls()
        for name, server_cfg in cfg.items():
            transport = server_cfg.get("transport")
            if transport not in ("stdio", "sse"):
                raise ValueError(
                    f"mcp_servers[{name!r}].transport must be 'stdio' or 'sse', "
                    f"got {transport!r}"
                )
            # 提取可靠性配置（与 MCP 字段同级）
            reliability = server_cfg.get("reliability", {}) or {}
            raw_overrides = server_cfg.get("risk_overrides") or {}
            if not isinstance(raw_overrides, dict):
                raise ValueError(
                    f"mcp_servers[{name!r}].risk_overrides must be a dict, "
                    f"got {type(raw_overrides).__name__}"
                )
            config = MCPServerConfig(
                name=name,
                transport=transport,
                command=list(server_cfg.get("command", []) or []),
                env=dict(server_cfg.get("env", {}) or {}),
                cwd=server_cfg.get("cwd"),
                url=server_cfg.get("url"),
                auth=server_cfg.get("auth", "none"),
                token=server_cfg.get("token"),
                auto_reconnect=reliability.get("auto_reconnect", True),
                max_reconnect_attempts=reliability.get("max_reconnect_attempts", 5),
                circuit_breaker_threshold=reliability.get("circuit_breaker_threshold", 3),
                risk_overrides={str(k): str(v) for k, v in raw_overrides.items()},
            )
            registry.register(config)
        return registry

    @classmethod
    def from_yaml(cls, path: str) -> MCPRegistry:
        """从 YAML 文件构造 registry（DESIGN §7.3 "配置示例"风格）"""
        import yaml

        with open(path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        if not isinstance(cfg, dict):
            raise ValueError(
                f"MCP YAML root must be a mapping, got {type(cfg).__name__}"
            )
        return cls.from_dict(cfg)


__all__ = [
    "MCPHealthStatus",
    "MCPRegistry",
    "MCPServerConfig",
    "Transport",
]
