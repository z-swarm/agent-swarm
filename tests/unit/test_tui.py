"""
@module tests.unit.test_tui
@brief  TUI 模块单测

覆盖:
  - TUISink 队列入队 / 满时 drop
  - TokenBudgetData.add_result 粗估 token
  - SwarmStatusData 时间状态字段
  - SwarmDashboardApp 在 5 秒内能完整启动并接收事件
"""

from __future__ import annotations

import time

import pytest

from agent_swarm.core.types import SessionEvent
from agent_swarm.tui import SwarmDashboardApp, TUISink
from agent_swarm.tui.app import (
    AgentInfo,
    MessageRow,
    SwarmStatusData,
    TaskRow,
    TokenBudgetData,
)

# ---------------------------------------------------------------------------
# TUISink
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tui_sink_enqueue_basic() -> None:
    """@brief 普通事件能入队"""
    sink = TUISink()
    evt = SessionEvent(
        event_name="task.created",
        session_id="s1",
        timestamp=time.time(),
        payload={"task_id": "T1"},
        seq=0,
    )
    await sink.consume(evt)
    assert sink.queue.qsize() == 1
    popped = sink.queue.get_nowait()
    assert popped.event_name == "task.created"


@pytest.mark.asyncio
async def test_tui_sink_drops_oldest_when_full() -> None:
    """@brief 队列满时丢最旧, 不阻塞业务"""
    sink = TUISink(maxsize=3)
    for i in range(5):
        await sink.consume(
            SessionEvent(
                event_name=f"e{i}",
                session_id="s",
                timestamp=time.time(),
                payload={},
                seq=i,
            )
        )
    assert sink.queue.qsize() == 3
    assert sink.dropped == 2
    # 验证留下的都是最新的 (e3, e4)
    names = [sink.queue.get_nowait().event_name for _ in range(3)]
    assert names == ["e2", "e3", "e4"]


# ---------------------------------------------------------------------------
# TokenBudgetData
# ---------------------------------------------------------------------------


def test_token_budget_add_result_estimation() -> None:
    """@brief 粗估: 1 token ≈ 4 chars"""
    b = TokenBudgetData()
    added = b.add_result("a" * 400)  # 100 tokens
    assert added == 100
    assert b.used_tokens == 100
    # 非字符串也支持——只验证是 >0 的合理值
    added2 = b.add_result({"k": "v" * 80})
    assert added2 > 0
    assert b.used_tokens > 100


def test_token_budget_add_result_minimum() -> None:
    """@brief 极短 result 至少算 1 token"""
    b = TokenBudgetData()
    assert b.add_result("") >= 1
    assert b.add_result("hi") >= 1


# ---------------------------------------------------------------------------
# SwarmStatusData
# ---------------------------------------------------------------------------


def test_swarm_status_uptime_before_start() -> None:
    """@brief 未开始时 uptime 为 '-'"""
    s = SwarmStatusData()
    assert s.uptime == "-"


def test_swarm_status_uptime_after_start() -> None:
    """@brief 启动后 uptime 持续累加"""
    s = SwarmStatusData()
    s.started_at = time.time() - 5
    assert "5." in s.uptime


def test_agent_info_default_state() -> None:
    """@brief AgentInfo 默认 idle"""
    a = AgentInfo(agent_id="x")
    assert a.status == "idle"
    assert a.tasks_done == 0


# ---------------------------------------------------------------------------
# 4 面板数据模型
# ---------------------------------------------------------------------------


def test_task_row_creation() -> None:
    r = TaskRow(task_id="T1", title="x", status="pending")
    assert r.owner == "-"
    assert r.status == "pending"


def test_message_row_truncation() -> None:
    """@brief MessageRow 字段验证"""
    m = MessageRow(timestamp="10:00", src="a", dst="b", preview="hello")
    assert m.preview == "hello"


# ---------------------------------------------------------------------------
# App 集成: 用 Pilot 验证 5 秒内能启动并接收事件
# ---------------------------------------------------------------------------


async def _drive_swarm_completed(app: SwarmDashboardApp) -> None:
    """
    @brief 同步注入 swarm.started + 4 个 task.* + swarm.completed

    @note 不真起 swarm——直接把事件塞进 sink queue, 验证 TUI 内部 routing
    """
    sink = app._sink  # noqa: SLF001
    base = time.time()
    # 1) swarm.started
    sink.queue.put_nowait(
        SessionEvent(
            event_name="swarm.started",
            session_id="test",
            timestamp=base,
            payload={
                "name": "demo",
                "agent_ids": ["researcher", "writer"],
                "task_count": 2,
            },
            seq=0,
        )
    )
    # 2) 4 个 task.* 事件
    for i, (name, _status, owner) in enumerate(
        [
            ("task.created", "pending", "-"),
            ("task.claimed", "in_progress", "researcher"),
            ("task.completed", "completed", "researcher"),
            ("task.failed", "failed", "writer"),
        ]
    ):
        sink.queue.put_nowait(
            SessionEvent(
                event_name=name,
                session_id="test",
                timestamp=base + i,
                payload={
                    "task_id": f"T{i}",
                    "title": f"task-{i}",
                    "agent_id": owner if owner != "-" else None,
                    "result": "x" * 200 if name == "task.completed" else None,
                },
                seq=i + 1,
            )
        )
    # 3) message.sent
    sink.queue.put_nowait(
        SessionEvent(
            event_name="message.sent",
            session_id="test",
            timestamp=base + 10,
            payload={"from": "researcher", "to": "writer", "subject": "hi"},
            seq=99,
        )
    )
    # 4) swarm.completed
    sink.queue.put_nowait(
        SessionEvent(
            event_name="swarm.completed",
            session_id="test",
            timestamp=base + 11,
            payload={"tasks_completed": 1, "tasks_failed": 1},
            seq=100,
        )
    )


@pytest.mark.asyncio
async def test_app_renders_full_view_within_5_seconds() -> None:
    """
    @brief W6 DoD: TUI 启动后 5 秒内显示完整 swarm 视图

    验证:
      - App 挂载成功
      - 4 面板都存在
      - 收到全部事件后, 状态数据正确
    """
    sink = TUISink()
    app = SwarmDashboardApp(sink, swarm_name="test-swarm")
    # 把所有事件预塞进 sink
    await _drive_swarm_completed(app)

    t0 = time.monotonic()
    async with app.run_test() as pilot:
        # 等到 is_finished=True, _pump_events 会在 2s 后 exit
        deadline = time.monotonic() + 5.0
        while not app._is_finished:  # noqa: SLF001
            if time.monotonic() > deadline:
                pytest.fail("TUI did not finish within 5 seconds")
            await pilot.pause(0.05)
        # 验证所有面板收到了数据
        assert app._status_data.name == "demo"  # noqa: SLF001
        assert app._status_data.state == "completed"  # noqa: SLF001
        assert len(app._status_data.agents) == 2
        assert app._status_data.tasks_completed == 1
        assert app._status_data.tasks_failed == 1
        assert len(app._task_panel.data) == 4  # noqa: SLF001
        assert len(app._msg_panel.data) == 1  # noqa: SLF001
        assert app._budget_data.used_tokens > 0  # noqa: SLF001
        # 5 秒 DoD 校验
        elapsed = time.monotonic() - t0
        assert elapsed < 5.0, f"full view took {elapsed:.2f}s > 5s"


@pytest.mark.asyncio
async def test_app_dispatches_task_completed_updates_budget() -> None:
    """@brief task.completed → budget 累加"""
    sink = TUISink()
    app = SwarmDashboardApp(sink)
    sink.queue.put_nowait(
        SessionEvent(
            event_name="task.completed",
            session_id="s",
            timestamp=time.time(),
            payload={"task_id": "T1", "title": "x", "result": "a" * 1000},
            seq=0,
        )
    )
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        assert app._budget_data.used_tokens >= 250  # noqa: SLF001
        assert app._budget_data.last_task_id == "T1"  # noqa: SLF001
        app._is_finished = True  # noqa: SLF001
        await pilot.pause(0.1)


# ---------------------------------------------------------------------------
# P3-3.7 (REVIEW-2026-06-19 §3.7) TUI 边界场景测试
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tui_handles_very_many_agents_without_crash() -> None:
    """大量 agent 涌入（100 个）——TUI 不崩不挂"""
    sink = TUISink(maxsize=200)
    app = SwarmDashboardApp(sink, swarm_name="many-agents")
    base = time.time()
    # 1) 先注入 swarm.started（带 100 个 agent_ids）——填充 status.agents
    await sink.consume(
        SessionEvent(
            event_name="swarm.started",
            session_id="s",
            timestamp=base,
            payload={
                "name": "many-agents",
                "agent_ids": [f"agent-{i:03d}" for i in range(100)],
                "task_count": 100,
            },
            seq=0,
        )
    )
    # 2) 再注入 100 个 task.claimed 事件
    for i in range(100):
        await sink.consume(
            SessionEvent(
                event_name="task.claimed",
                session_id="s",
                timestamp=base + 0.01 * (i + 1),
                payload={"task_id": f"T{i}", "title": f"task-{i}", "agent_id": f"agent-{i:03d}"},
                seq=i + 1,
            )
        )

    async with app.run_test() as pilot:
        # 给 pump 时间把队列处理完
        await pilot.pause(0.5)
        # 100 个 agent 都被注册
        assert len(app._status_data.agents) == 100  # noqa: SLF001
        # 100 个 task row（dict 长度）
        assert len(app._task_panel.data) == 100  # noqa: SLF001
        # 显式结束
        app._is_finished = True  # noqa: SLF001
        await pilot.pause(0.1)


@pytest.mark.asyncio
async def test_tui_handles_no_color_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """NO_COLOR=1 环境变量下 TUI 不应崩溃（颜色禁用是 accessibility 需求）"""
    monkeypatch.setenv("NO_COLOR", "1")
    sink = TUISink()
    app = SwarmDashboardApp(sink)
    await sink.consume(
        SessionEvent(
            event_name="swarm.started",
            session_id="s",
            timestamp=time.time(),
            payload={"name": "no-color", "agent_ids": ["a"], "task_count": 0},
            seq=0,
        )
    )

    async with app.run_test() as pilot:
        await pilot.pause(0.2)
        # App 仍然正常处理了事件
        assert app._status_data.name == "no-color"  # noqa: SLF001
        app._is_finished = True  # noqa: SLF001
        await pilot.pause(0.1)


@pytest.mark.asyncio
async def test_tui_quick_reconnect_drain_old_queue() -> None:
    """快速重连场景：注入一波事件 → 立即再注入新事件，TUI 不卡死旧事件"""
    sink = TUISink(maxsize=10)
    app = SwarmDashboardApp(sink, swarm_name="reconnect")

    # 第一波：50 个事件（队列 size=10 → 触发 drop 路径）
    for i in range(50):
        await sink.consume(
            SessionEvent(
                event_name="task.created",
                session_id="s1",
                timestamp=time.time(),
                payload={"task_id": f"T{i}", "title": f"t{i}"},
                seq=i,
            )
        )
    # 第二波：新的 session_id + 5 个事件
    for i in range(5):
        await sink.consume(
            SessionEvent(
                event_name="task.created",
                session_id="s2",
                timestamp=time.time(),
                payload={"task_id": f"T2_{i}", "title": f"t2_{i}"},
                seq=100 + i,
            )
        )

    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        # drop 计数 > 0（验证丢最旧逻辑生效）
        assert sink.dropped > 0
        # 新事件仍被收下（data 是 dict[str, TaskRow]）
        task_ids = set(app._task_panel.data.keys())  # noqa: SLF001
        assert any(t.startswith("T2_") for t in task_ids), "新 session 事件未进入"
        app._is_finished = True  # noqa: SLF001
        await pilot.pause(0.1)


@pytest.mark.asyncio
async def test_tui_handles_terminal_resize_event() -> None:
    """终端 resize 事件——TUI 不崩（resize 是常见边界）"""
    sink = TUISink()
    app = SwarmDashboardApp(sink)
    await sink.consume(
        SessionEvent(
            event_name="swarm.started",
            session_id="s",
            timestamp=time.time(),
            payload={"name": "resize", "agent_ids": ["a"], "task_count": 0},
            seq=0,
        )
    )

    async with app.run_test() as pilot:
        # 模拟 resize
        await pilot.resize_terminal(40, 20)  # noqa: SLF001
        await pilot.resize_terminal(200, 60)
        await pilot.pause(0.2)
        # 不应抛异常
        assert app._status_data.name == "resize"  # noqa: SLF001
        app._is_finished = True  # noqa: SLF001
        await pilot.pause(0.1)


@pytest.mark.asyncio
async def test_tui_handles_malformed_event_gracefully() -> None:
    """畸形事件（payload 缺字段）——TUI 不应崩"""
    sink = TUISink()
    app = SwarmDashboardApp(sink)
    # 缺 task_id 字段
    await sink.consume(
        SessionEvent(
            event_name="task.completed",
            session_id="s",
            timestamp=time.time(),
            payload={"title": "no-task-id"},  # 缺 task_id
            seq=0,
        )
    )
    # 缺 agent_ids
    await sink.consume(
        SessionEvent(
            event_name="swarm.started",
            session_id="s",
            timestamp=time.time(),
            payload={"name": "no-agents"},  # 缺 agent_ids
            seq=1,
        )
    )

    async with app.run_test() as pilot:
        await pilot.pause(0.2)
        # 关键：不抛 IndexError/KeyError 类的崩
        # App 仍处于有效状态
        assert app._status_data is not None  # noqa: SLF001
        app._is_finished = True  # noqa: SLF001
        await pilot.pause(0.1)
