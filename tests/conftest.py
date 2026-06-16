"""
@module tests.conftest
@brief  全局测试 fixture——LLM 录制+回放（DESIGN.md §17.4）

策略:
  1. 默认回放（fake_llm fixture）：测试用预先编排的脚本响应；零网络
  2. 录制模式（pytest --llm-record）：W1 暂未实现，留接口
  3. 真实模式（pytest --llm-real）：跳过 fake，使用真实 OpenAI
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pytest

from agent_swarm.core.types import LLMResponse, ToolCall, Turn
from agent_swarm.providers.base import LLMProvider

# ---------------------------------------------------------------------------
# pytest 配置
# ---------------------------------------------------------------------------


def pytest_addoption(parser: pytest.Parser) -> None:
    """允许 pytest --llm-real 切到真实 LLM"""
    parser.addoption(
        "--llm-real",
        action="store_true",
        default=False,
        help="use real LLM API instead of fake (slow + costs money)",
    )


# ---------------------------------------------------------------------------
# Fake LLM Provider——按脚本回放
# ---------------------------------------------------------------------------


@dataclass
class ScriptedResponse:
    """单次 LLM 回复的预设脚本"""

    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"
    tokens_prompt: int = 100
    tokens_completion: int = 50


class FakeLLMProvider(LLMProvider):
    """
    可编程的 LLM provider——按预设脚本顺序返回

    用法:
        fake = FakeLLMProvider(default_model="gpt-4o-mini")
        fake.script.append(ScriptedResponse(
            tool_calls=[ToolCall(id="c1", name="read_file",
                                 arguments={"path": "README.md"})],
            finish_reason="tool_use",
        ))
        fake.script.append(ScriptedResponse(content="Done.", finish_reason="stop"))
    """

    def __init__(
        self,
        default_model: str = "gpt-4o-mini",
        on_call: Callable[[list[Turn]], None] | None = None,
    ) -> None:
        self._default_model = default_model
        self.script: list[ScriptedResponse] = []
        self.calls: list[list[Turn]] = []  # 记录每次调用的 messages（便于断言）
        self._on_call = on_call

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
        # 复制 messages 以防外部 mutate
        self.calls.append(list(messages))
        if self._on_call:
            self._on_call(messages)

        if not self.script:
            # 默认兜底——避免脚本写漏导致死循环
            return LLMResponse(
                content="(fake llm: no more scripted responses)",
                tool_calls=[],
                finish_reason="stop",
                tokens_prompt=10,
                tokens_completion=10,
                model=model or self._default_model,
            )

        s = self.script.pop(0)
        # finish_reason 类型校正（types 里是 Literal[...]，运行期是字符串）
        fr = s.finish_reason  # type: ignore[assignment]
        return LLMResponse(
            content=s.content,
            tool_calls=list(s.tool_calls),
            finish_reason=fr,  # type: ignore[arg-type]
            tokens_prompt=s.tokens_prompt,
            tokens_completion=s.tokens_completion,
            model=model or self._default_model,
        )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_llm() -> FakeLLMProvider:
    """
    最常用 fixture：测试中编排 LLM 行为

        def test_xxx(fake_llm):
            fake_llm.script.append(ScriptedResponse(content="Hi", finish_reason="stop"))
            ...
    """
    return FakeLLMProvider()


@pytest.fixture
def use_real_llm(request: pytest.FixtureRequest) -> bool:
    """单测可读 --llm-real 标志（多用于 e2e）"""
    return bool(request.config.getoption("--llm-real"))


@pytest.fixture(autouse=True)
def _isolate_skill_registry():
    """
    W4-ZT4 修复：每个测试前后保护 SkillRegistry 内置项不被污染

    - 记录测试开始前的 skill id 集合
    - 测试结束时清理掉新增的（用户注册）skill
    - 内置 skill 在 review.py import 时注册——保留
    """
    from agent_swarm.skills.base import SkillRegistry

    snapshot = set(SkillRegistry._instances.keys())
    yield
    current = set(SkillRegistry._instances.keys())
    leaked = current - snapshot
    for sid in leaked:
        SkillRegistry.unregister(sid)
