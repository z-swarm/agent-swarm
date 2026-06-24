"""
@module agent_swarm.providers
@brief  Provider 工厂——W2 支持 OpenAI + Anthropic
"""

from agent_swarm.providers.anthropic_provider import AnthropicProvider
from agent_swarm.providers.base import LLMProvider
from agent_swarm.providers.openai_provider import OpenAIProvider


def get_provider(name: str, **kwargs) -> LLMProvider:
    """根据 provider 名构造 LLMProvider 实例"""
    if name == "openai":
        return OpenAIProvider(**kwargs)
    if name == "anthropic":
        return AnthropicProvider(**kwargs)
    raise ValueError(f"Unknown provider: {name!r} (W2 supports 'openai' and 'anthropic')")


__all__ = [
    "AnthropicProvider",
    "LLMProvider",
    "OpenAIProvider",
    "get_provider",
]
