"""
@module agent_swarm.mcp.adapter
@brief  W9-3 MCPToolAdapter——MCP tool → agent_swarm Tool 协议

DESIGN §7.3 MCPToolAdapter：
- name / description / parameters 来自 MCP tool schema
- invoke(arguments) 调 client.call_tool(name, args) → 返回 content
- 每个 MCP tool 自动获 ToolRisk 评估（默认 MEDIUM，可配置覆写）
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from agent_swarm.mcp.client import MCPClient
from agent_swarm.mcp.registry import MCPServerConfig

# ToolRisk 在 security.policy；adapter 不直接依赖，避免循环
ToolRiskStr = str  # "low" / "medium" / "high" / "critical"


@dataclass
class MCPToolAdapter:
    """
    单个 MCP tool 适配为 agent_swarm Tool 协议

    @note name / description / parameters 来自 MCP tools/list 响应
    @note invoke 调 client.call_tool 并把 content 序列化为字符串
          （MCP content 是 list[{"type": "text", "text": "..."}]; 取所有 text 拼接）
    """

    server_name: str
    mcp_tool_name: str
    description: str
    parameters: dict[str, Any]  # MCP tool inputSchema
    client: MCPClient  # type: ignore[valid-type]
    risk: ToolRiskStr = "medium"

    @property
    def name(self) -> str:
        """agent_swarm Tool.name——加 server 前缀避免跨 server 冲突"""
        return f"mcp.{self.server_name}.{self.mcp_tool_name}"

    async def invoke(self, arguments: dict[str, Any]) -> str:
        """调 MCP tools/call → 序列化 content 为字符串"""
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
    client: MCPClient,  # type: ignore[valid-type]
    risk_overrides: dict[str, ToolRiskStr] | None = None,
) -> list[MCPToolAdapter]:
    """
    异步工厂：从 MCP client 的 list_tools() 构造 MCPToolAdapter 列表

    @param risk_overrides tool_name → risk 字符串覆写；None 时从
           config.risk_overrides 读取（H1 fix：让 YAML 配置生效）

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
        adapters.append(MCPToolAdapter(
            server_name=server_name,
            mcp_tool_name=mcp_name,
            description=schema.get("description", ""),
            parameters=schema.get("inputSchema", {"type": "object"}),
            client=client,
            risk=overrides.get(mcp_name, "medium"),
        ))
    return adapters


# 向后兼容别名（W9-3 早期版本 + 验收脚本都引用此名）
await_build_tool_adapters = build_tool_adapters


__all__ = [
    "MCPToolAdapter",
    "ToolRiskStr",
    "await_build_tool_adapters",
    "build_tool_adapters",
]
