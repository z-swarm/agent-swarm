"""
@module tests.unit.test_prometheus_sink
@brief  W15 PrometheusSink 单元测试——DESIGN §15.3

覆盖:
  - 5 个核心指标定义 + 默认值
  - 事件 → 指标映射（task.* / llm.call_completed / cas.conflict / mcp.circuit_changed / approval.*）
  - 便捷 API: inc_task / add_llm_tokens / inc_cas_conflict / set_mcp_circuit
  - /metrics HTTP 端点文本格式
  - /healthz 端点
  - 异常事件不崩（consume 内部 catch）
"""

from __future__ import annotations

import pytest
from aiohttp.test_utils import TestClient, TestServer
from prometheus_client.parser import text_string_to_metric_families

from agent_swarm.core.types import SessionEvent
from agent_swarm.observability.prometheus_sink import (
    APPROVAL_PENDING_COUNT,
    CAS_CONFLICT_TOTAL,
    LLM_TOKENS_TOTAL,
    MCP_CIRCUIT_STATE,
    TASK_STATUS_LABELS,
    TASKS_TOTAL,
    PrometheusSink,
)

# ---------------------------------------------------------------------------
# 基本功能
# ---------------------------------------------------------------------------


def test_sink_constructs() -> None:
    sink = PrometheusSink()
    assert sink is not None
    assert len(TASK_STATUS_LABELS) == 5


def test_task_status_labels_complete() -> None:
    assert set(TASK_STATUS_LABELS) == {
        "pending",
        "blocked",
        "in_progress",
        "completed",
        "failed",
    }


def test_5_core_metrics_registered() -> None:
    """5 个核心指标在 /metrics 输出中都能找到"""
    sink = PrometheusSink()
    # 触发一次 inc 让指标被 emit
    sink.inc_task("pending")
    sink.add_llm_tokens("openai", "gpt-4o-mini", "prompt", 100)
    sink.inc_cas_conflict("task")
    sink.set_mcp_circuit("github", "open")
    sink.inc_approval_pending()

    body = _scrape(sink)
    # prometheus_client OpenMetrics 格式：每个 _total counter 拆出 _created
    # 所以 family.name 是基名（去 _total），sample.name 才带 _total
    families = list(text_string_to_metric_families(body))
    family_names = {f.name for f in families}
    # 验证 family 名（基名）存在
    assert "framework_tasks" in family_names
    assert "framework_llm_tokens" in family_names
    assert "framework_cas_conflict" in family_names
    assert "framework_mcp_circuit_state" in family_names
    assert "framework_approval_pending_count" in family_names
    # 同时验证带 _total 后缀的 sample 存在
    assert TASKS_TOTAL in body
    assert LLM_TOKENS_TOTAL in body
    assert CAS_CONFLICT_TOTAL in body
    assert MCP_CIRCUIT_STATE in body
    assert APPROVAL_PENDING_COUNT in body


# ---------------------------------------------------------------------------
# 事件 → 指标 映射
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_task_created_increments_pending() -> None:
    sink = PrometheusSink()
    await sink.consume(
        SessionEvent(
            event_name="task.created",
            session_id="s1",
            timestamp=0.0,
            payload={"task_id": "t1"},
        )
    )
    assert _get_counter(sink, TASKS_TOTAL, {"task_status": "pending"}) == 1.0


@pytest.mark.asyncio
async def test_task_completed_increments_completed() -> None:
    sink = PrometheusSink()
    await sink.consume(
        SessionEvent(
            event_name="task.completed",
            session_id="s1",
            timestamp=0.0,
            payload={"task_id": "t1"},
        )
    )
    assert _get_counter(sink, TASKS_TOTAL, {"task_status": "completed"}) == 1.0


@pytest.mark.asyncio
async def test_task_failed_increments_failed() -> None:
    sink = PrometheusSink()
    await sink.consume(
        SessionEvent(
            event_name="task.failed",
            session_id="s1",
            timestamp=0.0,
            payload={"task_id": "t1", "error": "boom"},
        )
    )
    assert _get_counter(sink, TASKS_TOTAL, {"task_status": "failed"}) == 1.0


@pytest.mark.asyncio
async def test_task_blocked_increments_blocked() -> None:
    sink = PrometheusSink()
    await sink.consume(
        SessionEvent(
            event_name="task.blocked",
            session_id="s1",
            timestamp=0.0,
            payload={"task_id": "t1"},
        )
    )
    assert _get_counter(sink, TASKS_TOTAL, {"task_status": "blocked"}) == 1.0


@pytest.mark.asyncio
async def test_task_claimed_increments_in_progress() -> None:
    sink = PrometheusSink()
    await sink.consume(
        SessionEvent(
            event_name="task.claimed",
            session_id="s1",
            timestamp=0.0,
            payload={"task_id": "t1", "agent_id": "a1"},
        )
    )
    assert _get_counter(sink, TASKS_TOTAL, {"task_status": "in_progress"}) == 1.0


@pytest.mark.asyncio
async def test_llm_call_completed_increments_tokens() -> None:
    sink = PrometheusSink()
    await sink.consume(
        SessionEvent(
            event_name="llm.call_completed",
            session_id="s1",
            timestamp=0.0,
            payload={"provider": "openai", "model": "gpt-4o-mini", "kind": "prompt", "tokens": 150},
        )
    )
    await sink.consume(
        SessionEvent(
            event_name="llm.call_completed",
            session_id="s1",
            timestamp=0.0,
            payload={
                "provider": "openai",
                "model": "gpt-4o-mini",
                "kind": "completion",
                "tokens": 50,
            },
        )
    )
    assert (
        _get_counter(
            sink,
            LLM_TOKENS_TOTAL,
            {
                "provider": "openai",
                "model": "gpt-4o-mini",
                "kind": "prompt",
            },
        )
        == 150.0
    )
    assert (
        _get_counter(
            sink,
            LLM_TOKENS_TOTAL,
            {
                "provider": "openai",
                "model": "gpt-4o-mini",
                "kind": "completion",
            },
        )
        == 50.0
    )


@pytest.mark.asyncio
async def test_llm_call_completed_zero_tokens_skipped() -> None:
    """tokens=0 不增计数（避免误上报）"""
    sink = PrometheusSink()
    await sink.consume(
        SessionEvent(
            event_name="llm.call_completed",
            session_id="s1",
            timestamp=0.0,
            payload={"provider": "openai", "model": "x", "kind": "prompt", "tokens": 0},
        )
    )
    body = _scrape(sink)
    # 不应该有该 label 的样本
    assert 'kind="prompt"' not in body or "framework_llm_tokens_total" not in body


@pytest.mark.asyncio
async def test_cas_conflict_increments() -> None:
    sink = PrometheusSink()
    await sink.consume(
        SessionEvent(
            event_name="cas.conflict",
            session_id="s1",
            timestamp=0.0,
            payload={"entity": "task", "version": 5},
        )
    )
    assert _get_counter(sink, CAS_CONFLICT_TOTAL, {"entity": "task"}) == 1.0


@pytest.mark.asyncio
async def test_mcp_circuit_state_changes() -> None:
    sink = PrometheusSink()
    await sink.consume(
        SessionEvent(
            event_name="mcp.circuit_changed",
            session_id="s1",
            timestamp=0.0,
            payload={"server": "github", "state": "closed"},
        )
    )
    assert _get_gauge(sink, MCP_CIRCUIT_STATE, {"server": "github"}) == 0.0
    await sink.consume(
        SessionEvent(
            event_name="mcp.circuit_changed",
            session_id="s1",
            timestamp=0.0,
            payload={"server": "github", "state": "open"},
        )
    )
    assert _get_gauge(sink, MCP_CIRCUIT_STATE, {"server": "github"}) == 1.0
    await sink.consume(
        SessionEvent(
            event_name="mcp.circuit_changed",
            session_id="s1",
            timestamp=0.0,
            payload={"server": "github", "state": "half_open"},
        )
    )
    assert _get_gauge(sink, MCP_CIRCUIT_STATE, {"server": "github"}) == 0.5


@pytest.mark.asyncio
async def test_approval_pending_increments_decrements() -> None:
    sink = PrometheusSink()
    await sink.consume(
        SessionEvent(
            event_name="approval.requested",
            session_id="s1",
            timestamp=0.0,
            payload={"approval_id": "a1"},
        )
    )
    await sink.consume(
        SessionEvent(
            event_name="approval.requested",
            session_id="s1",
            timestamp=0.0,
            payload={"approval_id": "a2"},
        )
    )
    assert _get_gauge(sink, APPROVAL_PENDING_COUNT, {}) == 2.0
    await sink.consume(
        SessionEvent(
            event_name="approval.resolved",
            session_id="s1",
            timestamp=0.0,
            payload={"approval_id": "a1"},
        )
    )
    assert _get_gauge(sink, APPROVAL_PENDING_COUNT, {}) == 1.0


# ---------------------------------------------------------------------------
# 便捷 API
# ---------------------------------------------------------------------------


def test_inc_task_validates_status() -> None:
    sink = PrometheusSink()
    sink.inc_task("completed")
    assert _get_counter(sink, TASKS_TOTAL, {"task_status": "completed"}) == 1.0


def test_inc_task_unknown_status_warns() -> None:
    sink = PrometheusSink()
    sink.inc_task("unknown_state")  # 不抛
    # 不应创建 unknown 标签的指标


def test_add_llm_tokens_zero_skipped() -> None:
    sink = PrometheusSink()
    sink.add_llm_tokens("openai", "gpt-4o-mini", "prompt", 0)
    body = _scrape(sink)
    assert 'kind="prompt"' not in body


def test_add_llm_tokens_increments() -> None:
    sink = PrometheusSink()
    sink.add_llm_tokens("anthropic", "claude-3-5", "completion", 200)
    assert (
        _get_counter(
            sink,
            LLM_TOKENS_TOTAL,
            {
                "provider": "anthropic",
                "model": "claude-3-5",
                "kind": "completion",
            },
        )
        == 200.0
    )


def test_inc_cas_conflict() -> None:
    sink = PrometheusSink()
    sink.inc_cas_conflict("message")
    assert _get_counter(sink, CAS_CONFLICT_TOTAL, {"entity": "message"}) == 1.0


def test_set_mcp_circuit() -> None:
    sink = PrometheusSink()
    sink.set_mcp_circuit("github", "closed")
    assert _get_gauge(sink, MCP_CIRCUIT_STATE, {"server": "github"}) == 0.0
    sink.set_mcp_circuit("github", "open")
    assert _get_gauge(sink, MCP_CIRCUIT_STATE, {"server": "github"}) == 1.0


def test_approval_inc_dec() -> None:
    sink = PrometheusSink()
    sink.inc_approval_pending()
    sink.inc_approval_pending()
    assert _get_gauge(sink, APPROVAL_PENDING_COUNT, {}) == 2.0
    sink.dec_approval_pending()
    assert _get_gauge(sink, APPROVAL_PENDING_COUNT, {}) == 1.0


# ---------------------------------------------------------------------------
# 错误处理
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consume_does_not_crash_on_unknown_event() -> None:
    sink = PrometheusSink()
    await sink.consume(
        SessionEvent(
            event_name="unknown.event",
            session_id="s1",
            timestamp=0.0,
        )
    )
    # 不抛


@pytest.mark.asyncio
async def test_consume_handles_malformed_payload() -> None:
    sink = PrometheusSink()
    # llm.call_completed 但 tokens 不是数字
    await sink.consume(
        SessionEvent(
            event_name="llm.call_completed",
            session_id="s1",
            timestamp=0.0,
            payload={"provider": "x", "model": "y", "kind": "prompt", "tokens": "not a number"},
        )
    )
    # 不抛


# ---------------------------------------------------------------------------
# /metrics HTTP 端点
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_metrics_endpoint_returns_text_format() -> None:
    sink = PrometheusSink()
    sink.inc_task("completed")
    app = sink.make_app(path="/metrics")
    async with TestServer(app) as server, TestClient(server) as client:
        resp = await client.get("/metrics")
        assert resp.status == 200
        text = await resp.text()
        # 解析为 metric families 验证格式合法
        families = list(text_string_to_metric_families(text))
        # 验证 family 基名（不是带 _total 的 sample 名）
        family_names = {f.name for f in families}
        assert "framework_tasks" in family_names


@pytest.mark.asyncio
async def test_healthz_endpoint() -> None:
    sink = PrometheusSink()
    app = sink.make_app(path="/metrics")
    async with TestServer(app) as server, TestClient(server) as client:
        resp = await client.get("/healthz")
        assert resp.status == 200
        text = await resp.text()
        assert text == "ok"


@pytest.mark.asyncio
async def test_metrics_content_type() -> None:
    sink = PrometheusSink()
    app = sink.make_app(path="/metrics")
    async with TestServer(app) as server, TestClient(server) as client:
        resp = await client.get("/metrics")
        ct = resp.headers.get("Content-Type", "")
        assert "text/plain" in ct


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _scrape(sink: PrometheusSink) -> str:
    """直接生成 /metrics 输出（不经 HTTP）"""
    from prometheus_client import generate_latest

    return generate_latest(sink.registry).decode("utf-8")


def _get_counter(sink: PrometheusSink, name: str, labels: dict[str, str]) -> float:
    """按 name（_total 结尾）找对应 sample

    @note prometheus_client 0.20+ OpenMetrics 格式：每个 Counter 拆出
          <name>_total 样本（值）+ <name>_created 样本（时间戳）——family.name
          是去掉 _total 的基名，所以样本里要按 sample.name=="<name>"
    """
    body = _scrape(sink)
    for family in text_string_to_metric_families(body):
        for sample in family.samples:
            if sample.name == name and dict(sample.labels) == labels:
                return sample.value
    return 0.0


def _get_gauge(sink: PrometheusSink, name: str, labels: dict[str, str]) -> float:
    body = _scrape(sink)
    for family in text_string_to_metric_families(body):
        for sample in family.samples:
            if sample.name == name and dict(sample.labels) == labels:
                return sample.value
    return 0.0
