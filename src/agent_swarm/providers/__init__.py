"""
@module agent_swarm.providers
@brief  Provider 工厂——W1 仅 OpenAI；后续按 provider 名分发
"""

from agent_swarm.providers.base import LLMProvider
from agent_swarm.providers.openai_provider import OpenAIProvider


def get_provider(name: str, **kwargs) -> LLMProvider:
    """根据 provider 名构造 LLMProvider 实例"""
    if name == "openai":
        return OpenAIProvider(**kwargs)
    raise ValueError(f"Unknown provider: {name!r} (W1 only supports 'openai')")


__all__ = ["LLMProvider", "OpenAIProvider", "get_provider"]
