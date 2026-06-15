"""
@module agent_swarm.providers.anthropic_provider
@brief  Anthropic Claude 适配器（W2）

W2 范围:
  - chat() with tools (Claude tool use 格式)
  - 不做流式（W4 上）
  - 通过 ANTHROPIC_API_KEY 环境变量获取 key

设计要点（DESIGN.md §9.1）:
  - Claude 把 system prompt 单独传，不混在 messages 里
  - tool 角色对应 Claude 的 user message + content[].type=tool_result
  - assistant 工具调用对应 content[].type=tool_use
  - finish_reason 归一化:
      "tool_use" → "tool_use"（W2 内部统一名）
      "end_turn" → "stop"
      "max_tokens" → "length"
"""

from __future__ import annotations

import logging
import os
from typing import Any

from anthropic import AsyncAnthropic

from agent_swarm.core.types import LLMResponse, ToolCall, Turn
from agent_swarm.providers.base import LLMProvider

log = logging.getLogger(__name__)


class AnthropicProvider(LLMProvider):
    """Claude 适配器——v4.2 推荐 claude-sonnet-4-6 / claude-opus-4-8"""

    def __init__(
        self,
        api_key: str | None = None,
        default_model: str = "claude-sonnet-4-6",
        base_url: str | None = None,
    ) -> None:
        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY not set—either pass api_key= or export it"
            )
        kwargs: dict[str, Any] = {"api_key": key}
        if base_url:
            kwargs["base_url"] = base_url
        self._client = AsyncAnthropic(**kwargs)
        self._default_model = default_model

    @property
    def default_model(self) -> str:
        return self._default_model

    async def chat(
        self,
        messages: list[Turn],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 2048,
        temperature: float = 0.0,
    ) -> LLMResponse:
        # 1) 把 system 单独抽出（Claude API 要求）
        system_prompt, dialogue = self._split_system(messages)

        # 2) 转换 dialogue（user/assistant/tool）→ Anthropic 格式
        anth_messages = [self._turn_to_anthropic(t) for t in dialogue]

        # 3) tools 转换：OpenAI 风格 {name, description, parameters}
        #    → Anthropic {name, description, input_schema}
        anth_tools = (
            [
                {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "input_schema": t.get("parameters", {"type": "object"}),
                }
                for t in tools
            ]
            if tools
            else None
        )

        # 4) 调用
        kwargs: dict[str, Any] = {
            "model": model or self._default_model,
            "messages": anth_messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if system_prompt:
            kwargs["system"] = system_prompt
        if anth_tools:
            kwargs["tools"] = anth_tools

        resp = await self._client.messages.create(**kwargs)

        # 5) 解析 content blocks——可能含 text 和 tool_use 两类
        content_text = ""
        tool_calls: list[ToolCall] = []
        for block in resp.content:
            btype = getattr(block, "type", None)
            if btype == "text":
                content_text += block.text
            elif btype == "tool_use":
                tool_calls.append(
                    ToolCall(
                        id=block.id,
                        name=block.name,
                        arguments=dict(block.input) if block.input else {},
                    )
                )

        # 6) finish_reason 归一化
        stop_reason = resp.stop_reason
        if stop_reason == "tool_use":
            finish_reason: Any = "tool_use"
        elif stop_reason == "end_turn":
            finish_reason = "stop"
        elif stop_reason == "max_tokens":
            finish_reason = "length"
        else:
            finish_reason = "stop"

        return LLMResponse(
            content=content_text,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            tokens_prompt=resp.usage.input_tokens if resp.usage else 0,
            tokens_completion=resp.usage.output_tokens if resp.usage else 0,
            model=resp.model,
        )

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------
    @staticmethod
    def _split_system(messages: list[Turn]) -> tuple[str, list[Turn]]:
        """从 messages 抽出 system prompt（Claude 单独参数）"""
        system_parts: list[str] = []
        dialogue: list[Turn] = []
        for m in messages:
            if m.role == "system":
                system_parts.append(m.content)
            else:
                dialogue.append(m)
        return ("\n\n".join(system_parts), dialogue)

    @staticmethod
    def _turn_to_anthropic(turn: Turn) -> dict[str, Any]:
        """
        Turn → Anthropic message dict

        Anthropic content block 类型:
          - text: 普通文本
          - tool_use: assistant 调用工具
          - tool_result: 工具执行结果（作为 user 消息发回）
        """
        if turn.role == "tool":
            # tool 角色 → Anthropic user message 的 tool_result block
            return {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": turn.tool_call_id or "",
                        "content": turn.content,
                    }
                ],
            }

        if turn.role == "assistant" and turn.tool_calls:
            # assistant + tool_calls → text + tool_use blocks
            blocks: list[dict[str, Any]] = []
            if turn.content:
                blocks.append({"type": "text", "text": turn.content})
            for tc in turn.tool_calls:
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": tc.id,
                        "name": tc.name,
                        "input": tc.arguments,
                    }
                )
            return {"role": "assistant", "content": blocks}

        # 纯 user / 纯 assistant
        return {"role": turn.role, "content": turn.content}
