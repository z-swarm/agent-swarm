"""
@module tests.unit.test_mcp_source_tier
@brief  W20-⑧ MCP source 分级测试——§16.3 #10 收紧

覆盖:
  - MCPSource 枚举 + SOURCE_TO_DEFAULT_RISK
  - validate_source: 必填 + 合法值
  - require_approval_for_source: HIGH 强制 Approval
  - bump_tool_risk_by_source: 取 max
"""

from __future__ import annotations

import pytest

from agent_swarm.security.mcp_source_tier import (
    SOURCE_TIER_POLICIES,
    SOURCE_TO_DEFAULT_RISK,
    MCPSource,
    bump_tool_risk_by_source,
    require_approval_for_source,
    validate_source,
)
from agent_swarm.security.policy import ToolRisk

# ---------------------------------------------------------------------------
# 基础
# ---------------------------------------------------------------------------


def test_source_to_default_risk() -> None:
    """W20-⑧ 默认分级——official=LOW / private=MEDIUM / community=HIGH"""
    assert SOURCE_TO_DEFAULT_RISK[MCPSource.OFFICIAL] == ToolRisk.LOW
    assert SOURCE_TO_DEFAULT_RISK[MCPSource.PRIVATE] == ToolRisk.MEDIUM
    assert SOURCE_TO_DEFAULT_RISK[MCPSource.COMMUNITY] == ToolRisk.HIGH


def test_source_policies_complete() -> None:
    """所有 source 都有策略"""
    assert set(SOURCE_TIER_POLICIES.keys()) == set(MCPSource)


# ---------------------------------------------------------------------------
# validate_source (强制必填)
# ---------------------------------------------------------------------------


def test_validate_source_none_raises() -> None:
    with pytest.raises(ValueError, match="source is required"):
        validate_source(None)


def test_validate_source_empty_raises() -> None:
    with pytest.raises(ValueError, match="source is required"):
        validate_source("")


def test_validate_source_invalid_raises() -> None:
    with pytest.raises(ValueError, match="invalid MCP source"):
        validate_source("random")


@pytest.mark.parametrize(
    "source",
    ["official", "community", "private"],
)
def test_validate_source_valid(source: str) -> None:
    assert validate_source(source) == MCPSource(source)


# ---------------------------------------------------------------------------
# require_approval_for_source
# ---------------------------------------------------------------------------


def test_official_no_approval() -> None:
    assert require_approval_for_source(MCPSource.OFFICIAL) is False


def test_private_no_approval() -> None:
    """MEDIUM 不强制 Approval"""
    assert require_approval_for_source(MCPSource.PRIVATE) is False


def test_community_requires_approval() -> None:
    """HIGH 必须 Approval——W20 §16.3 #10 收紧"""
    assert require_approval_for_source(MCPSource.COMMUNITY) is True


# ---------------------------------------------------------------------------
# bump_tool_risk_by_source
# ---------------------------------------------------------------------------


def test_bump_tool_risk_low_source_bumps_to_low() -> None:
    """official base=LOW → 仍 LOW"""
    assert bump_tool_risk_by_source(ToolRisk.LOW, "official") == ToolRisk.LOW


def test_bump_tool_risk_low_source_community() -> None:
    """official base=LOW + community → HIGH"""
    assert bump_tool_risk_by_source(ToolRisk.LOW, "community") == ToolRisk.HIGH


def test_bump_tool_risk_critical_stays() -> None:
    """base=CRITICAL 不会被下调"""
    assert bump_tool_risk_by_source(
        ToolRisk.CRITICAL, "official",
    ) == ToolRisk.CRITICAL


def test_bump_tool_risk_high_community() -> None:
    """base=HIGH + community=HIGH → HIGH"""
    assert bump_tool_risk_by_source(ToolRisk.HIGH, "community") == ToolRisk.HIGH


# ---------------------------------------------------------------------------
# MCPServerConfig source 必填
# ---------------------------------------------------------------------------


def test_mcp_server_config_requires_source() -> None:
    """MCPServerConfig.source 必须显式提供——YAML 缺此字段启动失败"""
    from agent_swarm.mcp.registry import MCPServerConfig
    with pytest.raises(ValueError, match="must be one of"):
        MCPServerConfig(
            name="test", transport="stdio",
            command=["echo"], source="random",  # type: ignore[arg-type]
        )


def test_mcp_server_config_default_source() -> None:
    """默认 source=community (安全侧——保守)"""
    from agent_swarm.mcp.registry import MCPServerConfig
    cfg = MCPServerConfig(
        name="test", transport="stdio",
        command=["echo"],
    )
    assert cfg.source == "community"


def test_mcp_server_config_official_source() -> None:
    from agent_swarm.mcp.registry import MCPServerConfig
    cfg = MCPServerConfig(
        name="anthropic", transport="sse",
        url="https://api.anthropic.com/mcp",
        source="official",
    )
    assert cfg.source == "official"
