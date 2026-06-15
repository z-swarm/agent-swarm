"""
@module agent_swarm.providers.openai_provider
@brief  OpenAI 适配器（W1 最小可用）

W1 范围：
  - chat() with tools (function calling)
  - 不做流式、连接池、circuit breaker（这些放 W2/W4）
  - 通过 OPENAI_API_KEY 环境变量获取 key
"""

from __future__ import annotations

import json
import os
from typing import Any

from openai import AsyncOpenAI

from agent_swarm.core.types import LLMResponse, ToolCall, Turn
from agent_swarm.providers.base import LLMProvider


class OpenAIProvider(LLMProvider):
    """OpenAI Chat Completions 适配器"""

    def __init__(
        self,
        api_key: str | None = None,
        default_model: str = "gpt-4o-mini",
        base_url: str | None = None,
    ) -> None:
        """
        @param api_key  显式传入；否则读 OPENAI_API_KEY 环境变量
        @param default_model 默认 gpt-4o-mini（cheap 适合 W1 验证）
        @param base_url 兼容 OpenAI 协议的服务端点（如 vLLM/Together AI）
        """
        key = api_key or os.environ.get("OPENAI_API_KEY")
        if not key:
            raise RuntimeError(
                "OPENAI_API_KEY not set—either pass api_key= or export it"
            )
        self._client = AsyncOpenAI(api_key=key, base_url=base_url)
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
        """调用 OpenAI Chat Completions——W1 同步阻塞模式"""
        # 1) Turn → OpenAI message 格式
        oai_messages = [self._turn_to_oai(t) for t in messages]

        # 2) tools schema 必须包装为 OpenAI function calling 格式
        oai_tools = (
            [{"type": "function", "function": t} for t in tools] if tools else None
        )

        # 3) 调用
        kwargs: dict[str, Any] = {
            "model": model or self._default_model,
            "messages": oai_messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if oai_tools:
            kwargs["tools"] = oai_tools

        resp = await self._client.chat.completions.create(**kwargs)
        choice = resp.choices[0]
        msg = choice.message

        # 4) 解析 tool_calls
        tool_calls: list[ToolCall] = []
        if msg.tool_calls:
            for tc in msg.tool_calls:
                # arguments 是 JSON 字符串，需要解析
                try:
                    args = json.loads(tc.function.arguments) if tc.function.arguments else {}
                except json.JSONDecodeError:
                    # LLM 偶尔会输出非法 JSON——保留原文供调试
                    args = {"_raw": tc.function.arguments}
                tool_calls.append(
                    ToolCall(id=tc.id, name=tc.function.name, arguments=args)
                )

        # finish_reason 归一化：OpenAI 用 "tool_calls" 而我们用 "tool_use"
        finish_reason: Any = choice.finish_reason
        if finish_reason == "tool_calls":
            finish_reason = "tool_use"
        elif finish_reason not in ("stop", "length", "content_filter"):
            finish_reason = "stop"  # 兜底

        usage = resp.usage
        return LLMResponse(
            content=msg.content or "",
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            tokens_prompt=usage.prompt_tokens if usage else 0,
            tokens_completion=usage.completion_tokens if usage else 0,
            model=resp.model,
        )

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------
    @staticmethod
    def _turn_to_oai(turn: Turn) -> dict[str, Any]:
        """Turn → OpenAI 消息 dict"""
        msg: dict[str, Any] = {"role": turn.role}

        if turn.role == "tool":
            # tool 消息必须含 tool_call_id
            msg["content"] = turn.content
            msg["tool_call_id"] = turn.tool_call_id or ""
        elif turn.role == "assistant" and turn.tool_calls:
            # assistant 带工具调用：content 可空，tool_calls 必填
            msg["content"] = turn.content or None
            msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                    },
                }
                for tc in turn.tool_calls
            ]
        else:
            msg["content"] = turn.content

        return msg
