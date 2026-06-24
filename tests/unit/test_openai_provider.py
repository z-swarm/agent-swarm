"""
@module tests.unit.test_openai_provider
@brief  OpenAIProvider 关键路径单元测试（B4 修复）

W1 e2e 全靠 FakeLLMProvider，OpenAIProvider 实际代码 21% 覆盖——
真实 LLM key 一跑就可能炸。这里覆盖：
  - _turn_to_oai 三类分支（system/user / assistant+tool_calls / tool）
  - chat() 主流程：mock OpenAI client，验证响应解析
  - 边界：非法 JSON arguments / finish_reason 归一化 / 缺 OPENAI_API_KEY
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from agent_swarm.core.types import ToolCall, Turn
from agent_swarm.providers.openai_provider import OpenAIProvider

# ---------------------------------------------------------------------------
# 构造与基础
# ---------------------------------------------------------------------------


def test_provider_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """没有 OPENAI_API_KEY 应抛 RuntimeError"""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        OpenAIProvider()


def test_provider_accepts_explicit_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """显式 api_key 参数优先于环境变量"""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    p = OpenAIProvider(api_key="sk-explicit", default_model="gpt-test")
    assert p.default_model == "gpt-test"


def test_provider_reads_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """从环境变量读取 key"""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    p = OpenAIProvider()
    assert p.default_model == "gpt-4o-mini"


# ---------------------------------------------------------------------------
# _turn_to_oai——纯函数转换（无网络）
# ---------------------------------------------------------------------------


def test_turn_to_oai_system() -> None:
    """system role 直接 content 透传"""
    out = OpenAIProvider._turn_to_oai(Turn(role="system", content="be helpful"))
    assert out == {"role": "system", "content": "be helpful"}


def test_turn_to_oai_user() -> None:
    """user role 同 system"""
    out = OpenAIProvider._turn_to_oai(Turn(role="user", content="hi"))
    assert out == {"role": "user", "content": "hi"}


def test_turn_to_oai_plain_assistant() -> None:
    """没有 tool_calls 的 assistant 走简单分支"""
    out = OpenAIProvider._turn_to_oai(Turn(role="assistant", content="ok"))
    assert out == {"role": "assistant", "content": "ok"}


def test_turn_to_oai_assistant_with_tool_calls() -> None:
    """assistant + tool_calls 必须包成 OpenAI function calling 格式"""
    tc = ToolCall(id="c1", name="read_file", arguments={"path": "README.md"})
    out = OpenAIProvider._turn_to_oai(Turn(role="assistant", content="", tool_calls=[tc]))
    assert out["role"] == "assistant"
    assert out["content"] is None  # 空 content 转 None（OpenAI 要求）
    assert len(out["tool_calls"]) == 1
    call = out["tool_calls"][0]
    assert call["id"] == "c1"
    assert call["type"] == "function"
    assert call["function"]["name"] == "read_file"
    # arguments 必须是 JSON 字符串而非 dict
    assert isinstance(call["function"]["arguments"], str)
    assert json.loads(call["function"]["arguments"]) == {"path": "README.md"}


def test_turn_to_oai_assistant_with_content_and_tool_calls() -> None:
    """assistant 同时有 content 和 tool_calls 时 content 保留"""
    tc = ToolCall(id="c1", name="read_file", arguments={})
    out = OpenAIProvider._turn_to_oai(
        Turn(role="assistant", content="thinking...", tool_calls=[tc])
    )
    assert out["content"] == "thinking..."


def test_turn_to_oai_tool() -> None:
    """tool role 必须含 tool_call_id"""
    out = OpenAIProvider._turn_to_oai(Turn(role="tool", content="file content", tool_call_id="c1"))
    assert out == {"role": "tool", "content": "file content", "tool_call_id": "c1"}


def test_turn_to_oai_tool_missing_id() -> None:
    """tool_call_id 缺失时不应抛异常——给空串保持 OpenAI 接受格式"""
    out = OpenAIProvider._turn_to_oai(Turn(role="tool", content="x"))
    assert out["tool_call_id"] == ""


def test_turn_to_oai_unicode_arguments() -> None:
    """非 ASCII 参数序列化时不应被 \\uXXXX 转义"""
    tc = ToolCall(id="c1", name="x", arguments={"path": "中文.md"})
    out = OpenAIProvider._turn_to_oai(Turn(role="assistant", content="", tool_calls=[tc]))
    args_str = out["tool_calls"][0]["function"]["arguments"]
    assert "中文" in args_str  # ensure_ascii=False 生效


# ---------------------------------------------------------------------------
# chat() 主流程——mock AsyncOpenAI client
# ---------------------------------------------------------------------------


def _make_mock_response(
    content: str = "",
    tool_calls: list[Any] | None = None,
    finish_reason: str = "stop",
    prompt_tokens: int = 100,
    completion_tokens: int = 50,
    model: str = "gpt-4o-mini",
) -> Any:
    """构造一个仿 OpenAI ChatCompletion 响应对象"""
    msg = SimpleNamespace(content=content, tool_calls=tool_calls)
    choice = SimpleNamespace(message=msg, finish_reason=finish_reason)
    usage = SimpleNamespace(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )
    return SimpleNamespace(choices=[choice], usage=usage, model=model)


def _make_oai_tool_call(call_id: str, name: str, arguments: str) -> Any:
    """构造一个仿 OpenAI tool_call 对象"""
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> OpenAIProvider:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    return OpenAIProvider()


async def test_chat_simple_text_response(provider: OpenAIProvider) -> None:
    """无工具调用——直接返回 content + token 统计"""
    fake_resp = _make_mock_response(content="hello world", finish_reason="stop")
    provider._client.chat.completions.create = AsyncMock(return_value=fake_resp)

    result = await provider.chat(messages=[Turn(role="user", content="say hi")])
    assert result.content == "hello world"
    assert result.tool_calls == []
    assert result.finish_reason == "stop"
    assert result.tokens_prompt == 100
    assert result.tokens_completion == 50


async def test_chat_with_tools_schema_wrapped(provider: OpenAIProvider) -> None:
    """tools 参数应被包装为 OpenAI {type: function, function: ...} 格式"""
    fake_resp = _make_mock_response(content="ok", finish_reason="stop")
    create_mock = AsyncMock(return_value=fake_resp)
    provider._client.chat.completions.create = create_mock

    schema = {
        "name": "read_file",
        "description": "read it",
        "parameters": {"type": "object"},
    }
    await provider.chat(
        messages=[Turn(role="user", content="x")],
        tools=[schema],
    )
    call_kwargs = create_mock.call_args.kwargs
    assert call_kwargs["tools"] == [{"type": "function", "function": schema}]


async def test_chat_no_tools_omits_tools_kwarg(provider: OpenAIProvider) -> None:
    """tools=None 时不应传 tools 参数（避免 OpenAI 严格模式拒收）"""
    fake_resp = _make_mock_response(content="ok")
    create_mock = AsyncMock(return_value=fake_resp)
    provider._client.chat.completions.create = create_mock

    await provider.chat(messages=[Turn(role="user", content="x")])
    assert "tools" not in create_mock.call_args.kwargs


async def test_chat_parses_tool_calls(provider: OpenAIProvider) -> None:
    """tool_calls 解析为 ToolCall 列表，arguments JSON 解码"""
    oai_tc = _make_oai_tool_call(
        call_id="call_1",
        name="read_file",
        arguments='{"path": "README.md"}',
    )
    fake_resp = _make_mock_response(
        content="",
        tool_calls=[oai_tc],
        finish_reason="tool_calls",
    )
    provider._client.chat.completions.create = AsyncMock(return_value=fake_resp)

    result = await provider.chat(messages=[Turn(role="user", content="read it")])
    assert len(result.tool_calls) == 1
    tc = result.tool_calls[0]
    assert tc.id == "call_1"
    assert tc.name == "read_file"
    assert tc.arguments == {"path": "README.md"}
    # finish_reason 应被归一化为 "tool_use"
    assert result.finish_reason == "tool_use"


async def test_chat_handles_invalid_json_arguments(provider: OpenAIProvider) -> None:
    """LLM 偶尔吐非法 JSON——不应崩，保留 _raw 字段供调试"""
    oai_tc = _make_oai_tool_call(
        call_id="call_1",
        name="read_file",
        arguments='{"path": broken',  # 非法
    )
    fake_resp = _make_mock_response(
        tool_calls=[oai_tc],
        finish_reason="tool_calls",
    )
    provider._client.chat.completions.create = AsyncMock(return_value=fake_resp)

    result = await provider.chat(messages=[Turn(role="user", content="x")])
    assert result.tool_calls[0].arguments == {"_raw": '{"path": broken'}


async def test_chat_handles_empty_arguments(provider: OpenAIProvider) -> None:
    """arguments 为空字符串时也应得到合法 dict"""
    oai_tc = _make_oai_tool_call(call_id="c1", name="x", arguments="")
    fake_resp = _make_mock_response(tool_calls=[oai_tc], finish_reason="tool_calls")
    provider._client.chat.completions.create = AsyncMock(return_value=fake_resp)
    result = await provider.chat(messages=[Turn(role="user", content="x")])
    assert result.tool_calls[0].arguments == {}


async def test_chat_finish_reason_passthrough(provider: OpenAIProvider) -> None:
    """已知 finish_reason 应原样保留"""
    for fr in ("stop", "length", "content_filter"):
        fake_resp = _make_mock_response(content="x", finish_reason=fr)
        provider._client.chat.completions.create = AsyncMock(return_value=fake_resp)
        result = await provider.chat(messages=[Turn(role="user", content="x")])
        assert result.finish_reason == fr


async def test_chat_finish_reason_unknown_falls_back_to_stop(
    provider: OpenAIProvider,
) -> None:
    """未知 finish_reason 兜底为 'stop'"""
    fake_resp = _make_mock_response(content="x", finish_reason="weird_new_reason")
    provider._client.chat.completions.create = AsyncMock(return_value=fake_resp)
    result = await provider.chat(messages=[Turn(role="user", content="x")])
    assert result.finish_reason == "stop"


async def test_chat_handles_missing_usage(provider: OpenAIProvider) -> None:
    """usage 为 None（某些 base_url 兼容服务可能不返回）时 token 计为 0"""
    msg = SimpleNamespace(content="ok", tool_calls=None)
    choice = SimpleNamespace(message=msg, finish_reason="stop")
    fake_resp = SimpleNamespace(choices=[choice], usage=None, model="x")
    provider._client.chat.completions.create = AsyncMock(return_value=fake_resp)

    result = await provider.chat(messages=[Turn(role="user", content="x")])
    assert result.tokens_prompt == 0
    assert result.tokens_completion == 0


async def test_chat_passes_model_override(provider: OpenAIProvider) -> None:
    """显式 model 参数应覆盖 default_model"""
    fake_resp = _make_mock_response()
    create_mock = AsyncMock(return_value=fake_resp)
    provider._client.chat.completions.create = create_mock

    await provider.chat(
        messages=[Turn(role="user", content="x")],
        model="gpt-4o",
    )
    assert create_mock.call_args.kwargs["model"] == "gpt-4o"


async def test_chat_uses_default_model_when_not_specified(
    provider: OpenAIProvider,
) -> None:
    """未传 model 时使用 default_model"""
    fake_resp = _make_mock_response()
    create_mock = AsyncMock(return_value=fake_resp)
    provider._client.chat.completions.create = create_mock

    await provider.chat(messages=[Turn(role="user", content="x")])
    assert create_mock.call_args.kwargs["model"] == "gpt-4o-mini"


async def test_chat_round_trip_with_tool_history(provider: OpenAIProvider) -> None:
    """完整对话历史（含 tool 角色）应正确序列化"""
    history = [
        Turn(role="system", content="you are helpful"),
        Turn(role="user", content="read README"),
        Turn(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id="c1", name="read_file", arguments={"path": "README.md"})],
        ),
        Turn(role="tool", content="# project", tool_call_id="c1"),
    ]
    fake_resp = _make_mock_response(content="done", finish_reason="stop")
    create_mock = AsyncMock(return_value=fake_resp)
    provider._client.chat.completions.create = create_mock

    await provider.chat(messages=history)
    sent = create_mock.call_args.kwargs["messages"]
    assert len(sent) == 4
    assert sent[0]["role"] == "system"
    assert sent[2]["tool_calls"][0]["function"]["name"] == "read_file"
    assert sent[3]["tool_call_id"] == "c1"
