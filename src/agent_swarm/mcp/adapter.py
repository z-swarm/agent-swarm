"""
@module agent_swarm.mcp.adapter
@brief  W9-3 MCPToolAdapter——MCP tool → agent_swarm Tool 协议

DESIGN §7.3 MCPToolAdapter：
- name / description / parameters 来自 MCP tool schema
- invoke(arguments) 调 client.call_tool(name, args) → 返回 content
- 每个 MCP tool 自动获 ToolRisk 评估（默认 MEDIUM，可配置覆写）
- P1-3.1 (REVIEW-2026-06-19 §3.1)：
  invoke() 必须先经 SecurityPolicy.check_tool()，再按 adapter.risk
  走二次 HIGH/CRITICAL → REQUIRE_APPROVAL 闸门；YAML 的 risk_overrides
  必须真正生效。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from agent_swarm.mcp.client import MCPClient
from agent_swarm.mcp.registry import MCPServerConfig

if TYPE_CHECKING:
    from agent_swarm.security.policy import SecurityPolicy

# ToolRisk 在 security.policy；adapter 不直接依赖（运行时延迟 import 避免循环）
ToolRiskStr = str  # "low" / "medium" / "high" / "critical"

# 风险等级字符串 → 内部比较用（无 SecurityPolicy 时仍可走适配器自带闸门）
_RISK_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}


@dataclass
class MCPToolAdapter:
    """
    单个 MCP tool 适配为 agent_swarm Tool 协议

    @note name / description / parameters 来自 MCP tools/list 响应
    @note invoke 调 client.call_tool 并把 content 序列化为字符串
          （MCP content 是 list[{"type": "text", "text": "..."}]; 取所有 text 拼接）
    @note P1-3.1 修复：构造时若注入 SecurityPolicy，invoke() 必须先过
          policy.check_tool()；再按 self.risk 做二次闸门——
          HIGH/CRITICAL 一律 REQUIRE_APPROVAL（拒绝静默放行）
    """

    server_name: str
    mcp_tool_name: str
    description: str
    parameters: dict[str, Any]  # MCP tool inputSchema
    client: MCPClient
    risk: ToolRiskStr = "medium"
    policy: SecurityPolicy | None = None  # P1-3.1：可选注入

    @property
    def name(self) -> str:
        """agent_swarm Tool.name——加 server 前缀避免跨 server 冲突"""
        return f"mcp.{self.server_name}.{self.mcp_tool_name}"

    async def invoke(self, arguments: dict[str, Any]) -> str:
        """
        调 MCP tools/call → 序列化 content 为字符串

        P1-3.1 防御深度（两道闸门）：
          1) SecurityPolicy.check_tool(self.name, arguments)
             —— 路径/命令注入/敏感文件黑名单 等通用规则
          2) self.risk 二次闸门
             —— HIGH/CRITICAL → REQUIRE_APPROVAL（无论 policy 怎么说）
             —— 解决"通用 policy 不知道 MCP 工具存在"的盲区
        """
        # 闸门 1：SecurityPolicy（如注入）
        if self.policy is not None:
            decision = self.policy.check_tool(self.name, arguments)
            if decision.decision == "DENY":
                return f"[error] policy denied: {decision.reason}"
            if decision.decision == "REQUIRE_APPROVAL":
                return f"[error] requires approval: {decision.reason} (risk={self.risk})"

        # 闸门 2：风险等级二次校验（解决通用 policy 不感知 MCP 工具的问题）
        risk_level = _RISK_ORDER.get(self.risk, 1)
        if risk_level >= _RISK_ORDER["high"]:
            return (
                f"[error] requires approval: tool {self.name} has risk={self.risk} "
                f"(set via risk_overrides)"
            )

        # 实际调 MCP
        content = await self.client.call_tool(self.mcp_tool_name, arguments)
        return _serialize_content(content)


def _serialize_content(content: Any) -> str:
    """MCP content → 字符串（agent_swarm Tool.invoke 返回 str）

    MCP 协议：content 是 list[{"type": "text", "text": "..."}] 或 str。
    取所有 text 块拼接；非 text 块 JSON 序列化。
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text" and "text" in item:
                    parts.append(str(item["text"]))
                else:
                    parts.append(json.dumps(item, ensure_ascii=False))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    # fallback：其他类型 JSON 序列化
    return json.dumps(content, ensure_ascii=False)


async def build_tool_adapters(
    server_name: str,
    config: MCPServerConfig,
    client: MCPClient,
    risk_overrides: dict[str, ToolRiskStr] | None = None,
    policy: SecurityPolicy | None = None,  # P1-3.1：可注入
) -> list[MCPToolAdapter]:
    """
    异步工厂：从 MCP client 的 list_tools() 构造 MCPToolAdapter 列表

    @param risk_overrides tool_name → risk 字符串覆写；None 时从
           config.risk_overrides 读取（H1 fix：让 YAML 配置生效）
    @param policy          注入的 SecurityPolicy；None 表示不强制走闸门
           （向后兼容旧测试 + Phase 1 路径）

    @note 原 await_build_tool_adapters 是这个函数的别名（向后兼容）
    """
    if not client.is_connected():
        await client.connect()
    schemas = await client.list_tools()
    overrides = dict(risk_overrides) if risk_overrides else dict(config.risk_overrides)
    adapters: list[MCPToolAdapter] = []
    for schema in schemas:
        mcp_name = schema.get("name", "")
        if not mcp_name:
            continue
        adapters.append(
            MCPToolAdapter(
                server_name=server_name,
                mcp_tool_name=mcp_name,
                description=schema.get("description", ""),
                parameters=schema.get("inputSchema", {"type": "object"}),
                client=client,
                risk=overrides.get(mcp_name, "medium"),
                policy=policy,
            )
        )
    return adapters


# 向后兼容别名（W9-3 早期版本 + 验收脚本都引用此名）
await_build_tool_adapters = build_tool_adapters


__all__ = [
    "MCPToolAdapter",
    "ToolRiskStr",
    "await_build_tool_adapters",
    "build_tool_adapters",
]
