"""
@module agent_swarm.security.mcp_source_tier
@brief  W20-⑧ MCP server source 分级——§16.3 #10 收紧

P3-PLAN-v2 W20 DoD ⑧:
  - MCPServerConfig.source 字段强制必填 (official/community/private)
  - 默认分级:
      official   → ToolRisk.LOW
      private    → ToolRisk.MEDIUM
      community  → ToolRisk.HIGH
  - MEDIUM 以上触发 Approval 流程

@note 与 W11 ApprovalGate 集成: HIGH/CRITICAL 工具需人工审批
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from agent_swarm.security.policy import ToolRisk


class MCPSource(Enum):
    """W20-⑧ MCP server 源类型"""

    OFFICIAL = "official"
    COMMUNITY = "community"
    PRIVATE = "private"


# 分级映射——DESIGN §16.3 #10
SOURCE_TO_DEFAULT_RISK: dict[MCPSource, ToolRisk] = {
    MCPSource.OFFICIAL: ToolRisk.LOW,
    MCPSource.PRIVATE: ToolRisk.MEDIUM,
    MCPSource.COMMUNITY: ToolRisk.HIGH,
}


@dataclass(frozen=True)
class SourceTierPolicy:
    """单条 source 分级策略"""

    source: MCPSource
    default_risk: ToolRisk
    require_approval: bool  # MEDIUM+ 时 True
    allow_in_production: bool
    notes: str


# 详细策略——可独立扩展
SOURCE_TIER_POLICIES: dict[MCPSource, SourceTierPolicy] = {
    MCPSource.OFFICIAL: SourceTierPolicy(
        source=MCPSource.OFFICIAL,
        default_risk=ToolRisk.LOW,
        require_approval=False,
        allow_in_production=True,
        notes="官方维护 (Anthropic / OpenAI / Microsoft 等)。低风险,直接启用。",
    ),
    MCPSource.PRIVATE: SourceTierPolicy(
        source=MCPSource.PRIVATE,
        default_risk=ToolRisk.MEDIUM,
        require_approval=False,  # MEDIUM 不强制审批
        allow_in_production=True,
        notes="团队自维护。中等风险,需要 code review。",
    ),
    MCPSource.COMMUNITY: SourceTierPolicy(
        source=MCPSource.COMMUNITY,
        default_risk=ToolRisk.HIGH,
        require_approval=True,  # HIGH 强制 Approval
        allow_in_production=False,  # 生产环境默认禁
        notes="社区贡献。高风险,生产环境必须人工审批 + 安全审计。",
    ),
}


def get_source_risk(source: str | MCPSource) -> ToolRisk:
    """取 source 对应默认 ToolRisk"""
    if isinstance(source, str):
        try:
            source = MCPSource(source)
        except ValueError as e:
            raise ValueError(
                f"unknown MCP source: {source!r}, expected one of {[s.value for s in MCPSource]}",
            ) from e
    return SOURCE_TO_DEFAULT_RISK[source]


def require_approval_for_source(source: str | MCPSource) -> bool:
    """该 source 是否需要 Approval 流程"""
    if isinstance(source, str):
        source = MCPSource(source)
    return SOURCE_TIER_POLICIES[source].require_approval


def validate_source(source: str | None) -> MCPSource:
    """
    校验 source 字段——W20 §16.3 #10 强制必填

    @raise ValueError  source 缺失或非法
    """
    if source is None or not str(source).strip():
        raise ValueError(
            "MCPServerConfig.source is required (W20 §16.3 #10). "
            "Must be one of: official / community / private",
        )
    try:
        return MCPSource(source)
    except ValueError as e:
        valid = ", ".join(s.value for s in MCPSource)
        raise ValueError(
            f"invalid MCP source {source!r}. Valid: {valid}",
        ) from e


def bump_tool_risk_by_source(
    base_risk: ToolRisk,
    source: str | MCPSource,
) -> ToolRisk:
    """
    根据 source 提升工具基础风险等级

    @note 规则: 取 max(base_risk, source_default_risk)
    """
    src_risk = get_source_risk(source)
    order = [ToolRisk.LOW, ToolRisk.MEDIUM, ToolRisk.HIGH, ToolRisk.CRITICAL]
    if order.index(src_risk) > order.index(base_risk):
        return src_risk
    return base_risk


__all__ = [
    "MCPSource",
    "SOURCE_TO_DEFAULT_RISK",
    "SOURCE_TIER_POLICIES",
    "SourceTierPolicy",
    "bump_tool_risk_by_source",
    "get_source_risk",
    "require_approval_for_source",
    "validate_source",
]
