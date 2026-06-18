"""单元测试：providers 包入口 get_provider()——P-3 修复后

覆盖 get_provider 工厂函数的全部分支,补上 providers/__init__.py
的覆盖率盲区(W6 收尾时 36% → 100%)
"""

from __future__ import annotations

import pytest

from agent_swarm.providers import get_provider
from agent_swarm.providers.anthropic_provider import AnthropicProvider
from agent_swarm.providers.base import LLMProvider
from agent_swarm.providers.openai_provider import OpenAIProvider


def test_get_provider_openai(monkeypatch: pytest.MonkeyPatch) -> None:
    """get_provider("openai") 返回 OpenAIProvider 实例"""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake-test")
    p = get_provider("openai", default_model="gpt-4o-mini")
    assert isinstance(p, OpenAIProvider)
    assert isinstance(p, LLMProvider)
    assert p.default_model == "gpt-4o-mini"


def test_get_provider_anthropic(monkeypatch: pytest.MonkeyPatch) -> None:
    """get_provider("anthropic") 返回 AnthropicProvider 实例"""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake")
    p = get_provider("anthropic", default_model="claude-sonnet-4-6")
    assert isinstance(p, AnthropicProvider)
    assert p.default_model == "claude-sonnet-4-6"


def test_get_provider_unknown_raises() -> None:
    """未知 provider 名应抛 ValueError,且 message 包含 provider 名"""
    with pytest.raises(ValueError, match="deepseek"):
        get_provider("deepseek", default_model="x")


def test_get_provider_openai_missing_key_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OpenAI 但 env 没设 key——透传 OpenAIProvider 的 RuntimeError"""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        get_provider("openai", default_model="gpt-4o-mini")
