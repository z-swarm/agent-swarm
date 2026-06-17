"""单元测试：TokenBudgetManager——W5 简化版"""

from __future__ import annotations

import pytest

from agent_swarm.core.token_budget import TokenBudgetManager


@pytest.fixture
def mgr() -> TokenBudgetManager:
    """gpt-4o-mini 默认 128k context"""
    return TokenBudgetManager(context_window=128_000)


def test_initial_state(mgr: TokenBudgetManager) -> None:
    """空 context 不会触发截断"""
    budget = mgr.calculate_usage("system", [])
    assert budget.needs_truncation is False
    assert budget.remaining_tokens > 0


def test_calculate_usage_includes_system_prompt(mgr: TokenBudgetManager) -> None:
    budget = mgr.calculate_usage("a" * 400, [])  # 400 chars ≈ 100 tokens
    assert budget.used_tokens >= 100


def test_calculate_usage_includes_messages(mgr: TokenBudgetManager) -> None:
    msgs = [
        {"role": "user", "content": "a" * 400},
        {"role": "assistant", "content": "b" * 800},
    ]
    budget = mgr.calculate_usage("", msgs)
    # 400 + 800 = 1200 chars ≈ 300 tokens + reserve
    assert budget.used_tokens >= 300


def test_calculate_usage_includes_tool_schemas(mgr: TokenBudgetManager) -> None:
    budget = mgr.calculate_usage(
        "",
        [{"role": "user", "content": "x"}],
        tool_schemas=[{"name": "tool", "description": "a" * 1000}],
    )
    # tool schema 也会消耗
    assert budget.used_tokens > 200


def test_needs_truncation_triggered(mgr: TokenBudgetManager) -> None:
    """超 80% 阈值触发截断"""
    # 构造 90% 窗口的内容
    big_content = "a" * (int(128_000 * 0.9) * 4)  # chars
    budget = mgr.calculate_usage("", [{"role": "user", "content": big_content}])
    assert budget.needs_truncation is True


def test_reserve_tokens_included(mgr: TokenBudgetManager) -> None:
    """used_tokens 应含 reserve"""
    budget = mgr.calculate_usage("", [])
    assert budget.used_tokens >= mgr.reserve_tokens


def test_remaining_tokens_never_negative(mgr: TokenBudgetManager) -> None:
    """超 100% 窗口——remaining 钳到 0"""
    big_content = "a" * (200_000 * 4)  # 远超 128k
    budget = mgr.calculate_usage("", [{"role": "user", "content": big_content}])
    assert budget.remaining_tokens == 0
    assert budget.needs_truncation is True


def test_smart_truncate_keeps_system_and_recent(mgr: TokenBudgetManager) -> None:
    msgs = [{"role": "system", "content": "s"}] + [
        {"role": "user", "content": f"m{i}"} for i in range(20)
    ]
    budget = mgr.calculate_usage("", msgs)  # 假设超
    budget.remaining_tokens = 0  # 强制 truncate
    truncated = mgr.smart_truncate(msgs, budget, keep_recent=5)
    # system + 5 recent
    assert len(truncated) == 6
    assert truncated[0]["role"] == "system"
    # 最后 5 条
    assert truncated[-1]["content"] == "m19"


def test_smart_truncate_no_truncation_needed(mgr: TokenBudgetManager) -> None:
    """budget.remaining > 0——不截断"""
    msgs = [{"role": "user", "content": "hi"}]
    budget = mgr.calculate_usage("", msgs)
    truncated = mgr.smart_truncate(msgs, budget, keep_recent=5)
    assert truncated == msgs


def test_smart_truncate_keeps_recent_when_under_recent_count(mgr: TokenBudgetManager) -> None:
    """消息数 < keep_recent——全保留"""
    msgs = [{"role": "user", "content": "a"}, {"role": "user", "content": "b"}]
    budget = mgr.calculate_usage("", msgs)
    budget.remaining_tokens = 0
    truncated = mgr.smart_truncate(msgs, budget, keep_recent=10)
    assert len(truncated) == 2


def test_limit_tool_result_truncates_long() -> None:
    mgr = TokenBudgetManager(context_window=128_000)
    big = "x" * 20_000
    result = mgr.limit_tool_result(big, max_chars=10_000)
    assert len(result) < 20_000
    assert "[truncated" in result


def test_limit_tool_result_passthrough_short() -> None:
    mgr = TokenBudgetManager(context_window=128_000)
    short = "x" * 100
    assert mgr.limit_tool_result(short, max_chars=10_000) == short


def test_limit_tool_result_default_max_chars() -> None:
    mgr = TokenBudgetManager(context_window=128_000)
    result = mgr.limit_tool_result("x" * 20_000)  # 默认 max=10000
    assert len(result) < 20_000


def test_chinese_char_count() -> None:
    """中文每字符 utf-8 3 字节——除以 4 应得合理 token 数"""
    mgr = TokenBudgetManager(context_window=128_000)
    # 100 个中文字符 = 300 bytes / 4 = 75 tokens（粗估）
    budget = mgr.calculate_usage("", [{"role": "user", "content": "中" * 100}])
    # 实际算法 len("中"*100) = 100 chars; encode utf-8 = 300 bytes; // 4 = 75
    # 但当前实现是 len(text.encode("utf-8")) // 4 = 300 // 4 = 75
    # 加上 reserve 4096
    assert budget.used_tokens >= 4096 + 75
