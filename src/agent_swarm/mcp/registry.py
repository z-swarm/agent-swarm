"""
@module agent_swarm.mcp.registry
@brief  W9-1 MCPRegistry + MCPServerConfig——DESIGN §7.3

W9 范围：先落地注册表 + 配置数据类，客户端/适配器 W9-2/W9-3 接入。
@note W9-1 不连真 server；只做配置管理 + 校验。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

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


class MCPRegistry:
    """
    MCP 服务器注册表——DESIGN §7.3

    W9-1 范围：管理 server 配置（register / get / list / remove）
    W9-2+ 扩展：连接管理（connect / disconnect / list_tools / shutdown）

    @note W9-1 不开 client 字段；W9-2 加 `clients: dict[str, MCPClient]`
    """

    def __init__(self) -> None:
        self._servers: dict[str, MCPServerConfig] = {}

    def register(self, config: MCPServerConfig) -> None:
        """
        注册一个 MCP server 配置

        @raise ValueError name 重复
        """
        if config.name in self._servers:
            raise ValueError(
                f"MCP server {config.name!r} already registered"
            )
        self._servers[config.name] = config

    def get(self, name: str) -> MCPServerConfig:
        """按 name 取配置；不存在抛 KeyError"""
        if name not in self._servers:
            raise KeyError(f"MCP server {name!r} not registered")
        return self._servers[name]

    def try_get(self, name: str) -> MCPServerConfig | None:
        """按 name 取配置；不存在返回 None（不抛）"""
        return self._servers.get(name)

    def list_names(self) -> list[str]:
        """列所有已注册 server 名（按注册顺序）"""
        return list(self._servers.keys())

    def list_all(self) -> list[MCPServerConfig]:
        """列所有配置对象"""
        return list(self._servers.values())

    def remove(self, name: str) -> bool:
        """注销；返回是否真注销了一个"""
        return self._servers.pop(name, None) is not None

    def __len__(self) -> int:
        return len(self._servers)

    def __contains__(self, name: str) -> bool:
        return name in self._servers

    # ------------------------------------------------------------------
    # 工厂：from_dict / from_yaml——DESIGN §7.3 YAML 配置示例的解析
    # ------------------------------------------------------------------
    @classmethod
    def from_dict(cls, cfg: dict[str, Any]) -> "MCPRegistry":
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
            )
            registry.register(config)
        return registry

    @classmethod
    def from_yaml(cls, path: str) -> "MCPRegistry":
        """从 YAML 文件构造 registry（DESIGN §7.3 "配置示例"风格）"""
        import yaml

        with open(path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        if not isinstance(cfg, dict):
            raise ValueError(
                f"MCP YAML root must be a mapping, got {type(cfg).__name__}"
            )
        return cls.from_dict(cfg)


__all__ = ["MCPRegistry", "MCPServerConfig", "Transport"]
