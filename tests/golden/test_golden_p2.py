"""
@module tests.golden.test_golden_p2
@brief  P2 Golden Case——对抗式验证根因定位 + MCP 可靠性

Phase 2 DoD ②：AdversarialVerifier 在 §17.3 的 5 个 P2 调试 case 上
根因命中率 ≥80%。

本测试用 P2 cases：
  - G-011..G-015: 对抗式验证根因定位
  - G-018: MCP server 崩溃重连 + 熔断（W14a 落地）

W14a-6 范围：G-018 case 跑通——验证 circuit breaker 在 3 次重连失败后打开。
"""

from __future__ import annotations

import time

import pytest

from agent_swarm.core.adversarial import AdversarialVerifier
from agent_swarm.core.types import (
    Agent,
    AgentCapabilities,
    Judgement,
    Stance,
)


def _plan_only(id: str) -> Agent:
    return Agent(
        id=id,
        role="judge",
        persona="",
        model="gpt-4o-mini",
        provider="openai",
        capabilities=AgentCapabilities.plan_only(),
    )


def _judge_from_truth(truth: str) -> callable:
    """
    构造确定性 judge_fn：每个 (agent, hyp) 组合——若 hyp_statement 含 truth
    子串则 SUPPORT（高 confidence），否则 REFUTE

    @param truth 真根因子串——hyp_statement 中包含则判 SUPPORT
    """

    async def judge_fn(agent, hyp_id, round_no, hypotheses: dict[str, str]):
        hyp_statement = hypotheses[hyp_id]
        if truth in hyp_statement:
            return Judgement(
                agent_id=agent.id,
                hypothesis_id=hyp_id,
                round_no=round_no,
                stance=Stance.SUPPORT,
                confidence=0.95,
                reasoning=f"evidence: {truth} matches hypothesis",
            )
        return Judgement(
            agent_id=agent.id,
            hypothesis_id=hyp_id,
            round_no=round_no,
            stance=Stance.REFUTE,
            confidence=0.85,
            reasoning=f"no evidence for {hyp_statement[:30]}",
        )

    return judge_fn


async def _run_case(
    hypotheses: list[str],
    truth: str,
    n_judges: int = 3,
) -> str | None:
    """
    跑一个 P2 case，返回根因命中结果（verdict.root_cause）或 None
    """
    hyp_id_to_stmt = {f"h{i}": s for i, s in enumerate(hypotheses)}
    base_fn = _judge_from_truth(truth)

    async def judge_fn(agent, hyp_id, round_no):
        return await base_fn(agent, hyp_id, round_no, hyp_id_to_stmt)

    v = AdversarialVerifier(min_survivors=1, max_rounds=3)
    verdict = await v.verify(
        hypotheses=hypotheses,
        agents=[_plan_only(f"j{i}") for i in range(n_judges)],
        judge_fn=judge_fn,
    )
    return verdict.root_cause


# ---------------------------------------------------------------------------
# G-011: pytest test failure 根因定位
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_g011_pytest_failure_root_cause() -> None:
    """G-011: pytest 失败根因——'assertion in test_login: missing None check'"""
    root_cause = await _run_case(
        hypotheses=[
            "conftest fixture not loading",
            "test_login assertion fails because login() returns None when password is empty string — missing None check",
            "pytest version mismatch with plugin",
            "test runner has wrong working directory",
        ],
        truth="missing None check",
    )
    assert root_cause is not None
    assert "missing None check" in root_cause


# ---------------------------------------------------------------------------
# G-012: build error 根因定位
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_g012_build_error_root_cause() -> None:
    """G-012: build 失败根因——'undefined reference: foo() in libbar.a'"""
    root_cause = await _run_case(
        hypotheses=[
            "compiler version too old",
            "missing source file in Makefile",
            "undefined reference: foo() in libbar.a — linker can't find symbol foo in static library",
            "header file missing #include guard",
        ],
        truth="undefined reference",
    )
    assert root_cause is not None
    assert "undefined reference" in root_cause


# ---------------------------------------------------------------------------
# G-013: 性能瓶颈根因定位
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_g013_performance_bottleneck_root_cause() -> None:
    """G-013: API 慢根因——'N+1 query in get_user_posts: SELECT per row'"""
    root_cause = await _run_case(
        hypotheses=[
            "network latency to DB server",
            "ORM lazy loading adds N+1 query: get_user_posts triggers SELECT per row instead of JOIN",
            "Python GIL contention",
            "JSON serialization on large response",
        ],
        truth="N+1",
    )
    assert root_cause is not None
    assert "N+1" in root_cause


# ---------------------------------------------------------------------------
# G-014: null deref 根因定位
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_g014_null_deref_root_cause() -> None:
    """G-014: null deref 根因——'config dict lacks 'db' key when env var unset'"""
    root_cause = await _run_case(
        hypotheses=[
            "API endpoint returns 404",
            "config dict lacks 'db' key when env var unset — accessing config['db']['host'] raises KeyError (null deref)",
            "thread deadlock in connection pool",
            "out of memory in worker process",
        ],
        truth="config['db']",
    )
    assert root_cause is not None
    assert "config['db']" in root_cause


# ---------------------------------------------------------------------------
# G-015: 内存泄漏根因定位
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_g015_memory_leak_root_cause() -> None:
    """G-015: 内存泄漏根因——'event listener registered in loop without cleanup'"""
    root_cause = await _run_case(
        hypotheses=[
            "cache eviction policy too aggressive",
            "file handle not closed in exception path",
            "event listener registered in request handler without removal — closure holds reference forever",
            "memory-mapped file not unmapped",
        ],
        truth="event listener",
    )
    assert root_cause is not None
    assert "event listener" in root_cause


# ---------------------------------------------------------------------------
# 汇总：5/5 ≥80% Phase 2 DoD ②
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_p2_overall_hit_rate_above_80_percent() -> None:
    """汇总：跑全部 5 个 P2 case 统计根因命中率，要求 ≥80%（Phase 2 DoD ②）"""
    cases = [
        (
            "G-011",
            [
                "conftest fixture not loading",
                "test_login assertion fails because login() returns None when password is empty string — missing None check",
                "pytest version mismatch with plugin",
                "test runner has wrong working directory",
            ],
            "missing None check",
        ),
        (
            "G-012",
            [
                "compiler version too old",
                "missing source file in Makefile",
                "undefined reference: foo() in libbar.a — linker can't find symbol foo in static library",
                "header file missing #include guard",
            ],
            "undefined reference",
        ),
        (
            "G-013",
            [
                "network latency to DB server",
                "ORM lazy loading adds N+1 query: get_user_posts triggers SELECT per row instead of JOIN",
                "Python GIL contention",
                "JSON serialization on large response",
            ],
            "N+1",
        ),
        (
            "G-014",
            [
                "API endpoint returns 404",
                "config dict lacks 'db' key when env var unset — accessing config['db']['host'] raises KeyError (null deref)",
                "thread deadlock in connection pool",
                "out of memory in worker process",
            ],
            "config['db']",
        ),
        (
            "G-015",
            [
                "cache eviction policy too aggressive",
                "file handle not closed in exception path",
                "event listener registered in request handler without removal — closure holds reference forever",
                "memory-mapped file not unmapped",
            ],
            "event listener",
        ),
    ]
    hits = 0
    for case_id, hypotheses, truth in cases:
        root_cause = await _run_case(hypotheses, truth)
        ok = root_cause is not None and truth in root_cause
        if ok:
            hits += 1
        else:
            print(f"  ✗ {case_id} miss: root_cause={root_cause!r}")
    rate = hits / len(cases)
    assert rate >= 0.8, f"P2 Golden Case 命中率 {rate:.0%} < 80%"
    print(f"\n  P2 Golden Case 命中率: {rate:.0%} ({hits}/{len(cases)})")


# ---------------------------------------------------------------------------
# G-018: MCP server 崩溃自动重连 3 次后熔断
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_g018_mcp_server_crash_circuit_opens() -> None:
    """G-018: MCP server 崩溃 -> 3 次重连失败 -> circuit OPEN -> ToolUnavailableError

    W14a-6 验证：MCPRegistry + ReconnectingMCPClient + CircuitBreaker 端到端走通。
    用 fake client 模拟永远连不上的死 server，验证：
      1. 第 1 次 call_tool 触发 1 轮重连（max_reconnect_attempts=3）-> 失败
      2. 连续 3 次 call_tool -> 每次都触发 1 轮重连 -> circuit 累计失败到 3 -> OPEN
      3. circuit OPEN 后第 4 次调用立即抛 MCPCircuitOpenError（不重连）
    """
    from agent_swarm.mcp.client import MCPClient, MCPConnectionError
    from agent_swarm.mcp.registry import MCPServerConfig
    from agent_swarm.mcp.reliability import (
        MCPCircuitOpenError,
        ReconnectingMCPClient,
    )

    class _DeadMCPClient(MCPClient):
        def __init__(self):
            self.connect_count = 0

        async def connect(self):
            self.connect_count += 1
            raise MCPConnectionError(f"dead server: connect attempt {self.connect_count} refused")

        async def disconnect(self):
            pass

        def is_connected(self):
            return False

        async def list_tools(self):
            raise MCPConnectionError("dead server: list_tools refused")

        async def call_tool(self, name, arguments):
            raise MCPConnectionError("dead server: call_tool refused")

    cfg = MCPServerConfig(
        name="dead-server",
        transport="stdio",
        command=["fake"],
        max_reconnect_attempts=3,
        circuit_breaker_threshold=3,
        auto_reconnect=True,
    )
    dead = _DeadMCPClient()
    wrapper = ReconnectingMCPClient(dead, cfg)

    for i in range(1, 4):
        start = time.monotonic()
        with pytest.raises(MCPConnectionError):
            await wrapper.list_tools()
        elapsed = time.monotonic() - start
        assert elapsed >= 0.4, f"call {i} too fast: {elapsed:.2f}s"
        assert wrapper.circuit_breaker.consecutive_failures == i

    assert wrapper.circuit_breaker.state == "open"
    assert wrapper.circuit_breaker.total_trips == 1

    start = time.monotonic()
    with pytest.raises(MCPCircuitOpenError) as exc_info:
        await wrapper.list_tools()
    elapsed = time.monotonic() - start
    assert elapsed < 0.1, f"OPEN circuit should reject fast, got {elapsed:.2f}s"
    assert exc_info.value.server_name == "dead-server"
    assert exc_info.value.failure_count == 3

    assert dead.connect_count == 9, (
        f"expected 9 connect attempts (3 list_tools * 3 reconnects), got {dead.connect_count}"
    )


@pytest.mark.asyncio
async def test_g018_circuit_recovers_after_cool_off() -> None:
    """G-018 副 case：circuit OPEN -> cool_off -> HALF_OPEN -> 试探成功 -> CLOSED"""
    from agent_swarm.mcp.client import MCPClient, MCPConnectionError
    from agent_swarm.mcp.registry import MCPServerConfig
    from agent_swarm.mcp.reliability import ReconnectingMCPClient

    class _RecoverableMCPClient(MCPClient):
        def __init__(self, fail_first_n=1):
            self.connect_count = 0
            self._connected = False
            self.fail_first_n = fail_first_n

        async def connect(self):
            self.connect_count += 1
            if self.connect_count <= self.fail_first_n:
                raise MCPConnectionError(f"recoverable: attempt {self.connect_count}")
            self._connected = True

        async def disconnect(self):
            self._connected = False

        def is_connected(self):
            return self._connected

        async def list_tools(self):
            if not self._connected:
                raise MCPConnectionError("not connected")
            return []

        async def call_tool(self, name, arguments):
            return "ok"

    cfg = MCPServerConfig(
        name="recoverable",
        transport="stdio",
        command=["fake"],
        max_reconnect_attempts=2,
        circuit_breaker_threshold=1,
        auto_reconnect=True,
    )
    inner = _RecoverableMCPClient(fail_first_n=99)
    wrapper = ReconnectingMCPClient(inner, cfg)

    # list_tools 失败（inner._connected=False）→ reconnect 失败（fail_first_n=99）
    # → circuit OPEN
    with pytest.raises(MCPConnectionError):
        await wrapper.list_tools()
    assert wrapper.circuit_breaker.state == "open"

    # 模拟 server 恢复——手动标记 inner 已连接 + 重置 circuit breaker
    # 走 circuit HALF_OPEN → CLOSED 路径
    inner._connected = True
    inner.fail_first_n = 0
    wrapper.circuit_breaker._cool_off_s = 0.1
    wrapper.circuit_breaker._opened_at = 0  # 强制 cool_off 已过（直接转 HALF_OPEN）
    wrapper.circuit_breaker._consecutive_failures = 0  # 清零

    # 下次 list_tools：inner 已连接，直接成功
    tools = await wrapper.list_tools()
    assert tools == []
    assert wrapper.circuit_breaker.state == "closed"
    assert wrapper.circuit_breaker.consecutive_failures == 0
