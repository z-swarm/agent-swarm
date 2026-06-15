"""
@module agent_swarm.providers.base
@brief  LLM Provider 抽象（W1 最小子集）

DESIGN.md §9.1 完整版含 chat_stream / count_tokens / pricing 等
W1 只引入 chat()——验证最短链路
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from agent_swarm.core.types import LLMResponse, Turn


class LLMProvider(ABC):
    """LLM 后端统一接口——W1 子集"""

    @abstractmethod
    async def chat(
        self,
        messages: list[Turn],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 2048,
        temperature: float = 0.0,
    ) -> LLMResponse:
        """
        @brief 发起一次 LLM 对话

        @param messages 对话历史（含 system）
        @param tools    工具 schema 列表，OpenAI function calling 格式
        @param model    覆盖默认模型
        @param max_tokens / temperature 标准参数

        @return LLMResponse 含 content / tool_calls / token 统计
        """
        ...

    @property
    @abstractmethod
    def default_model(self) -> str:
        """默认模型 id（无 model 参数时使用）"""
        ...
