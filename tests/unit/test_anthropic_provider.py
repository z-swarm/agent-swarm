"""
@module tests.unit.test_anthropic_provider
@brief  AnthropicProvider 关键路径单元测试

覆盖（参照 B4 OpenAI 模式）:
  - _turn_to_anthropic 三类分支（system 拆分 / assistant+tool_use / tool_result）
  - chat() 主流程：mock client，验证响应解析 + finish_reason 归一化
  - 边界：缺 ANTHROPIC_API_KEY / 缺 usage / 多个 content block
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from agent_swarm.core.types import ToolCall, Turn
from agent_swarm.providers.anthropic_provider import AnthropicProvider

# ---------------------------------------------------------------------------
# 构造与基础
# ---------------------------------------------------------------------------


def test_provider_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        AnthropicProvider()


def test_provider_accepts_explicit_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    p = AnthropicProvider(api_key="sk-ant-x", default_model="claude-haiku-4-5-20251001")
    assert p.default_model == "claude-haiku-4-5-20251001"


def test_provider_reads_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-env")
    p = AnthropicProvider()
    assert p.default_model == "claude-sonnet-4-6"


# ---------------------------------------------------------------------------
# _split_system / _turn_to_anthropic
# ---------------------------------------------------------------------------


def test_split_system_single() -> None:
    sys, dlg = AnthropicProvider._split_system(
        [Turn(role="system", content="be helpful"), Turn(role="user", content="hi")]
    )
    assert sys == "be helpful"
    assert len(dlg) == 1 and dlg[0].role == "user"


def test_split_system_multiple_concat() -> None:
    """多个 system turn 用 \\n\\n 拼接"""
    sys, _ = AnthropicProvider._split_system(
        [
            Turn(role="system", content="rule 1"),
            Turn(role="system", content="rule 2"),
            Turn(role="user", content="x"),
        ]
    )
    assert sys == "rule 1\n\nrule 2"


def test_split_system_no_system() -> None:
    sys, dlg = AnthropicProvider._split_system([Turn(role="user", content="hi")])
    assert sys == ""
    assert len(dlg) == 1


def test_turn_to_anthropic_user() -> None:
    out = AnthropicProvider._turn_to_anthropic(Turn(role="user", content="hi"))
    assert out == {"role": "user", "content": "hi"}


def test_turn_to_anthropic_plain_assistant() -> None:
    out = AnthropicProvider._turn_to_anthropic(Turn(role="assistant", content="ok"))
    assert out == {"role": "assistant", "content": "ok"}


def test_turn_to_anthropic_assistant_with_tool_use() -> None:
    """assistant + tool_calls → content blocks 含 tool_use"""
    tc = ToolCall(id="t1", name="read_file", arguments={"path": "x.md"})
    out = AnthropicProvider._turn_to_anthropic(
        Turn(role="assistant", content="thinking...", tool_calls=[tc])
    )
    assert out["role"] == "assistant"
    blocks = out["content"]
    assert len(blocks) == 2
    # 第一个是 text
    assert blocks[0] == {"type": "text", "text": "thinking..."}
    # 第二个是 tool_use
    assert blocks[1]["type"] == "tool_use"
    assert blocks[1]["id"] == "t1"
    assert blocks[1]["name"] == "read_file"
    assert blocks[1]["input"] == {"path": "x.md"}


def test_turn_to_anthropic_assistant_tool_use_no_text() -> None:
    """空 content + 工具调用——不应有空 text block"""
    tc = ToolCall(id="t1", name="x", arguments={})
    out = AnthropicProvider._turn_to_anthropic(
        Turn(role="assistant", content="", tool_calls=[tc])
    )
    blocks = out["content"]
    assert len(blocks) == 1
    assert blocks[0]["type"] == "tool_use"


def test_turn_to_anthropic_tool_result() -> None:
    """tool 角色 → user message + tool_result block"""
    out = AnthropicProvider._turn_to_anthropic(
        Turn(role="tool", content="file content", tool_call_id="t1")
    )
    assert out["role"] == "user"
    blocks = out["content"]
    assert len(blocks) == 1
    assert blocks[0] == {
        "type": "tool_result",
        "tool_use_id": "t1",
        "content": "file content",
    }


def test_turn_to_anthropic_tool_result_missing_id() -> None:
    """tool_call_id 缺失时给空字符串，不抛异常"""
    out = AnthropicProvider._turn_to_anthropic(Turn(role="tool", content="x"))
    assert out["content"][0]["tool_use_id"] == ""


# ---------------------------------------------------------------------------
# chat() 主流程
# ---------------------------------------------------------------------------


def _make_mock_response(
    text_blocks: list[str] | None = None,
    tool_use_blocks: list[dict[str, Any]] | None = None,
    stop_reason: str = "end_turn",
    input_tokens: int = 100,
    output_tokens: int = 50,
    model: str = "claude-sonnet-4-6",
) -> Any:
    blocks: list[Any] = []
    for txt in text_blocks or []:
        blocks.append(SimpleNamespace(type="text", text=txt))
    for tu in tool_use_blocks or []:
        blocks.append(
            SimpleNamespace(
                type="tool_use",
                id=tu["id"],
                name=tu["name"],
                input=tu["input"],
            )
        )
    usage = SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens)
    return SimpleNamespace(
        content=blocks,
        stop_reason=stop_reason,
        usage=usage,
        model=model,
    )


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> AnthropicProvider:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    return AnthropicProvider()


async def test_chat_simple_text(provider: AnthropicProvider) -> None:
    fake = _make_mock_response(text_blocks=["hello world"], stop_reason="end_turn")
    provider._client.messages.create = AsyncMock(return_value=fake)

    res = await provider.chat(messages=[Turn(role="user", content="hi")])
    assert res.content == "hello world"
    assert res.tool_calls == []
    assert res.finish_reason == "stop"
    assert res.tokens_prompt == 100
    assert res.tokens_completion == 50


async def test_chat_concatenates_multiple_text_blocks(
    provider: AnthropicProvider,
) -> None:
    """Claude 偶尔输出多个 text block——应被拼接"""
    fake = _make_mock_response(text_blocks=["part 1 ", "part 2"])
    provider._client.messages.create = AsyncMock(return_value=fake)
    res = await provider.chat(messages=[Turn(role="user", content="x")])
    assert res.content == "part 1 part 2"


async def test_chat_parses_tool_use(provider: AnthropicProvider) -> None:
    fake = _make_mock_response(
        text_blocks=["I will read it"],
        tool_use_blocks=[
            {"id": "tu_1", "name": "read_file", "input": {"path": "README.md"}}
        ],
        stop_reason="tool_use",
    )
    provider._client.messages.create = AsyncMock(return_value=fake)

    res = await provider.chat(messages=[Turn(role="user", content="x")])
    assert res.finish_reason == "tool_use"
    assert len(res.tool_calls) == 1
    tc = res.tool_calls[0]
    assert tc.id == "tu_1"
    assert tc.name == "read_file"
    assert tc.arguments == {"path": "README.md"}


async def test_chat_passes_system_prompt_separately(
    provider: AnthropicProvider,
) -> None:
    """system turn 应被抽到 kwargs.system，不混在 messages 中"""
    fake = _make_mock_response(text_blocks=["ok"])
    create_mock = AsyncMock(return_value=fake)
    provider._client.messages.create = create_mock

    await provider.chat(
        messages=[
            Turn(role="system", content="be brief"),
            Turn(role="user", content="hi"),
        ]
    )
    kwargs = create_mock.call_args.kwargs
    assert kwargs["system"] == "be brief"
    # messages 里没有 system
    assert all(m["role"] != "system" for m in kwargs["messages"])


async def test_chat_omits_system_when_empty(provider: AnthropicProvider) -> None:
    """没 system 时不应传 system 参数"""
    fake = _make_mock_response(text_blocks=["ok"])
    create_mock = AsyncMock(return_value=fake)
    provider._client.messages.create = create_mock

    await provider.chat(messages=[Turn(role="user", content="hi")])
    assert "system" not in create_mock.call_args.kwargs


async def test_chat_tools_schema_converted(provider: AnthropicProvider) -> None:
    """OpenAI 风格 tools → Anthropic input_schema 字段"""
    fake = _make_mock_response(text_blocks=["ok"])
    create_mock = AsyncMock(return_value=fake)
    provider._client.messages.create = create_mock

    await provider.chat(
        messages=[Turn(role="user", content="x")],
        tools=[
            {
                "name": "read_file",
                "description": "read it",
                "parameters": {"type": "object", "properties": {}},
            }
        ],
    )
    sent_tools = create_mock.call_args.kwargs["tools"]
    assert len(sent_tools) == 1
    assert sent_tools[0]["name"] == "read_file"
    assert sent_tools[0]["description"] == "read it"
    assert sent_tools[0]["input_schema"] == {"type": "object", "properties": {}}


async def test_chat_no_tools_omits_tools_kwarg(provider: AnthropicProvider) -> None:
    fake = _make_mock_response(text_blocks=["ok"])
    create_mock = AsyncMock(return_value=fake)
    provider._client.messages.create = create_mock
    await provider.chat(messages=[Turn(role="user", content="x")])
    assert "tools" not in create_mock.call_args.kwargs


async def test_chat_finish_reason_max_tokens_normalized(
    provider: AnthropicProvider,
) -> None:
    fake = _make_mock_response(text_blocks=["..."], stop_reason="max_tokens")
    provider._client.messages.create = AsyncMock(return_value=fake)
    res = await provider.chat(messages=[Turn(role="user", content="x")])
    assert res.finish_reason == "length"


async def test_chat_finish_reason_unknown_falls_back_to_stop(
    provider: AnthropicProvider,
) -> None:
    fake = _make_mock_response(text_blocks=["x"], stop_reason="some_new_reason")
    provider._client.messages.create = AsyncMock(return_value=fake)
    res = await provider.chat(messages=[Turn(role="user", content="x")])
    assert res.finish_reason == "stop"


async def test_chat_handles_missing_usage(provider: AnthropicProvider) -> None:
    fake = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="ok")],
        stop_reason="end_turn",
        usage=None,
        model="x",
    )
    provider._client.messages.create = AsyncMock(return_value=fake)
    res = await provider.chat(messages=[Turn(role="user", content="x")])
    assert res.tokens_prompt == 0
    assert res.tokens_completion == 0


async def test_chat_round_trip_with_tool_history(provider: AnthropicProvider) -> None:
    """完整对话历史（system + tool_use + tool_result）应正确序列化"""
    history = [
        Turn(role="system", content="you are helpful"),
        Turn(role="user", content="read README"),
        Turn(
            role="assistant",
            content="",
            tool_calls=[
                ToolCall(id="tu1", name="read_file", arguments={"path": "README.md"})
            ],
        ),
        Turn(role="tool", content="# project", tool_call_id="tu1"),
    ]
    fake = _make_mock_response(text_blocks=["done"])
    create_mock = AsyncMock(return_value=fake)
    provider._client.messages.create = create_mock

    await provider.chat(messages=history)
    kwargs = create_mock.call_args.kwargs
    assert kwargs["system"] == "you are helpful"
    sent = kwargs["messages"]
    assert len(sent) == 3  # user / assistant / user(tool_result)
    # assistant 的 tool_use block
    assert sent[1]["content"][0]["type"] == "tool_use"
    # tool_result 包在 user message
    assert sent[2]["role"] == "user"
    assert sent[2]["content"][0]["type"] == "tool_result"
    assert sent[2]["content"][0]["tool_use_id"] == "tu1"


async def test_chat_passes_model_override(provider: AnthropicProvider) -> None:
    fake = _make_mock_response(text_blocks=["x"])
    create_mock = AsyncMock(return_value=fake)
    provider._client.messages.create = create_mock
    await provider.chat(
        messages=[Turn(role="user", content="x")],
        model="claude-opus-4-8",
    )
    assert create_mock.call_args.kwargs["model"] == "claude-opus-4-8"


async def test_chat_tool_use_with_empty_input(provider: AnthropicProvider) -> None:
    """tool_use.input=None 时应得到空 dict 而非崩溃"""
    fake = _make_mock_response(
        tool_use_blocks=[{"id": "tu1", "name": "x", "input": None}],
        stop_reason="tool_use",
    )
    provider._client.messages.create = AsyncMock(return_value=fake)
    res = await provider.chat(messages=[Turn(role="user", content="x")])
    assert res.tool_calls[0].arguments == {}
