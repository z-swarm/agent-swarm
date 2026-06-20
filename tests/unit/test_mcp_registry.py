"""单元测试：MCPRegistry + MCPServerConfig（W9-1 / DESIGN §7.3）"""

from __future__ import annotations

import pytest
import yaml

from agent_swarm.mcp import MCPRegistry, MCPServerConfig

# ---------------------------------------------------------------------------
# MCPServerConfig 校验
# ---------------------------------------------------------------------------


def test_stdio_config_minimal() -> None:
    """stdio 最小配置：name + transport + command"""
    c = MCPServerConfig(
        name="fs", transport="stdio",
        command=["npx", "-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
    )
    assert c.name == "fs"
    assert c.command == ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
    assert c.env == {}
    assert c.cwd is None
    assert c.auto_reconnect is True
    assert c.circuit_breaker_threshold == 3


def test_sse_config_minimal() -> None:
    """sse 最小配置：name + transport + url"""
    c = MCPServerConfig(name="internal-db", transport="sse", url="https://mcp/db")
    assert c.url == "https://mcp/db"
    assert c.auth == "none"
    assert c.token is None


def test_sse_bearer_requires_token() -> None:
    """sse auth=bearer 必填 token"""
    with pytest.raises(ValueError, match="bearer requires 'token'"):
        MCPServerConfig(name="x", transport="sse", url="https://x", auth="bearer")


def test_stdio_with_url_rejected() -> None:
    """stdio 不应设 url（字段冲突）"""
    with pytest.raises(ValueError, match="should not set 'url'"):
        MCPServerConfig(
            name="x", transport="stdio",
            command=["x"], url="https://x",
        )


def test_sse_with_command_rejected() -> None:
    """sse 不应设 command"""
    with pytest.raises(ValueError, match="should not set 'command'"):
        MCPServerConfig(
            name="x", transport="sse", url="https://x",
            command=["x"],
        )


def test_empty_name_rejected() -> None:
    with pytest.raises(ValueError, match="name must be non-empty"):
        MCPServerConfig(name="", transport="stdio", command=["x"])


def test_stdio_empty_command_rejected() -> None:
    with pytest.raises(ValueError, match="non-empty 'command'"):
        MCPServerConfig(name="x", transport="stdio", command=[])


def test_sse_empty_url_rejected() -> None:
    with pytest.raises(ValueError, match="non-empty 'url'"):
        MCPServerConfig(name="x", transport="sse", url="")


def test_invalid_transport_rejected() -> None:
    with pytest.raises(ValueError, match="must be stdio or sse"):
        MCPServerConfig(name="x", transport="http", command=["x"])


# ---------------------------------------------------------------------------
# MCPRegistry
# ---------------------------------------------------------------------------


def test_registry_register_and_get() -> None:
    r = MCPRegistry()
    c = MCPServerConfig(name="fs", transport="stdio", command=["x"])
    r.register(c)
    assert r.get("fs") is c
    assert r.try_get("fs") is c
    assert r.try_get("missing") is None


def test_registry_duplicate_registration_rejected() -> None:
    r = MCPRegistry()
    c = MCPServerConfig(name="fs", transport="stdio", command=["x"])
    r.register(c)
    with pytest.raises(ValueError, match="already registered"):
        r.register(c)


def test_registry_get_missing_raises_keyerror() -> None:
    r = MCPRegistry()
    with pytest.raises(KeyError, match="not registered"):
        r.get("nope")


def test_registry_list_and_remove() -> None:
    r = MCPRegistry()
    r.register(MCPServerConfig(name="a", transport="stdio", command=["x"]))
    r.register(MCPServerConfig(name="b", transport="stdio", command=["y"]))
    assert r.list_names() == ["a", "b"]
    assert r.list_all() and len(r.list_all()) == 2
    assert r.remove("a") is True
    assert r.remove("ghost") is False
    assert r.list_names() == ["b"]


def test_registry_dunder_methods() -> None:
    r = MCPRegistry()
    r.register(MCPServerConfig(name="a", transport="stdio", command=["x"]))
    assert len(r) == 1
    assert "a" in r
    assert "b" not in r


# ---------------------------------------------------------------------------
# from_dict / from_yaml
# ---------------------------------------------------------------------------


def test_from_dict_two_servers() -> None:
    """DESIGN §7.3 配置示例：filesystem + internal-db"""
    cfg = {
        "filesystem": {
            "transport": "stdio",
            "command": ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
            "env": {},
            "reliability": {
                "auto_reconnect": True,
                "max_reconnect_attempts": 5,
                "circuit_breaker_threshold": 3,
            },
        },
        "internal-db": {
            "transport": "sse",
            "url": "https://mcp.internal/db",
            "auth": "bearer",
            "token": "${MCP_DB_TOKEN}",
        },
    }
    r = MCPRegistry.from_dict(cfg)
    assert r.list_names() == ["filesystem", "internal-db"]
    fs = r.get("filesystem")
    assert fs.command[0] == "npx"
    assert fs.auto_reconnect is True
    db = r.get("internal-db")
    assert db.url == "https://mcp.internal/db"
    assert db.auth == "bearer"
    assert db.token == "${MCP_DB_TOKEN}"  # SecretManager 引用，非明文


def test_from_dict_rejects_invalid_transport() -> None:
    with pytest.raises(ValueError, match="must be 'stdio' or 'sse'"):
        MCPRegistry.from_dict({"bad": {"transport": "http", "command": ["x"]}})


def test_from_yaml_round_trip(tmp_path) -> None:
    """YAML 配置 round-trip（DESIGN §7.3 示例）"""
    p = tmp_path / "mcp.yaml"
    p.write_text(yaml.safe_dump({
        "github": {
            "transport": "stdio",
            "command": ["npx", "-y", "@modelcontextprotocol/server-github"],
            "env": {"GITHUB_TOKEN": "${GITHUB_TOKEN}"},
            "risk_overrides": {"create_issue": "HIGH"},
            "reliability": {"max_reconnect_attempts": 5},
        },
    }), encoding="utf-8")
    r = MCPRegistry.from_yaml(str(p))
    g = r.get("github")
    assert g.transport == "stdio"
    assert g.env["GITHUB_TOKEN"] == "${GITHUB_TOKEN}"
    assert g.max_reconnect_attempts == 5


# ---------------------------------------------------------------------------
# H1 fix: risk_overrides 字段 + YAML 解析（H1 regression）
# ---------------------------------------------------------------------------


def test_risk_overrides_field_default_empty() -> None:
    """MCPServerConfig.risk_overrides 默认 {}（H1 fix）"""
    c = MCPServerConfig(name="x", transport="stdio", command=["x"])
    assert c.risk_overrides == {}


def test_from_dict_parses_risk_overrides() -> None:
    """YAML risk_overrides.create_issue: high 加载后生效（H1 fix）"""
    cfg = {
        "github": {
            "transport": "stdio",
            "command": ["x"],
            "risk_overrides": {
                "create_issue": "high",
                "create_pull_request": "critical",
            },
        },
    }
    r = MCPRegistry.from_dict(cfg)
    gh = r.get("github")
    assert gh.risk_overrides == {
        "create_issue": "high",
        "create_pull_request": "critical",
    }


def test_from_dict_rejects_non_dict_risk_overrides() -> None:
    """risk_overrides 必须是 dict（fail-fast）"""
    cfg = {
        "github": {
            "transport": "stdio",
            "command": ["x"],
            "risk_overrides": "high",  # 错类型
        },
    }
    with pytest.raises(ValueError, match="risk_overrides must be a dict"):
        MCPRegistry.from_dict(cfg)
