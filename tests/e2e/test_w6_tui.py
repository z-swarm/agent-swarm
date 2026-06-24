"""
@module tests.e2e.test_w6_tui
@brief  W6 TUI e2e（DESIGN.md §17.1 W6 DoD）

DoD 校验项:
  - agent-swarm tui <yaml> 可启动（不要求真渲染, mock 事件路径）
  - 启动后 5 秒内能显示完整 swarm 视图
  - 4 面板 (Status/Tasks/Messages/Budget) 都被事件填充

策略: 复用 tests.unit.test_tui 的 TUI 内部组件验证路径,
       单独 e2e 验证 CLI 子命令的入口存在 + TUISink 注册到全局 bus 的链路通
"""

from __future__ import annotations

import time

import pytest

from agent_swarm.core.types import SessionEvent
from agent_swarm.observability import (
    ObservabilityBus,
    set_global_bus,
)
from agent_swarm.tui import SwarmDashboardApp, TUISink


@pytest.fixture(autouse=True)
def _reset_global_bus():
    """@brief 每个 e2e 前清掉全局 bus——避免跨测试污染"""
    set_global_bus(None)
    yield
    set_global_bus(None)


@pytest.mark.asyncio
async def test_tui_sink_registers_into_global_bus() -> None:
    """@brief TUISink 能注册到全局 ObservabilityBus, 收到 emit 事件"""
    bus = ObservabilityBus()
    sink = TUISink()
    bus.register_sink(sink)
    set_global_bus(bus)

    from agent_swarm.observability import emit

    await emit("task.created", "e2e-session", {"task_id": "T-e2e", "title": "x"})

    # TUISink 应已收到
    assert sink.queue.qsize() == 1
    evt = sink.queue.get_nowait()
    assert evt.event_name == "task.created"
    assert evt.payload["task_id"] == "T-e2e"


@pytest.mark.asyncio
async def test_tui_app_full_lifecycle_in_5s() -> None:
    """
    @brief W6 DoD: 5 秒内完成 swarm 生命周期渲染

    模拟 1 个 task.created → claimed → completed + swarm.completed 的全流程
    """
    sink = TUISink()
    bus = ObservabilityBus()
    bus.register_sink(sink)
    set_global_bus(bus)

    app = SwarmDashboardApp(sink, swarm_name="e2e-swarm")

    # 预塞事件
    base = time.time()
    for i, (name, payload) in enumerate(
        [
            ("swarm.started", {"name": "e2e", "agent_ids": ["a1"], "task_count": 1}),
            ("task.created", {"task_id": "T1", "title": "demo"}),
            ("task.claimed", {"task_id": "T1", "agent_id": "a1"}),
            ("message.sent", {"from": "a1", "to": "a1", "subject": "self"}),
            ("task.completed", {"task_id": "T1", "result": "x" * 400}),
            ("swarm.completed", {"tasks_completed": 1, "tasks_failed": 0}),
        ]
    ):
        sink.queue.put_nowait(
            SessionEvent(
                event_name=name,
                session_id="e2e",
                timestamp=base + i,
                payload=payload,
                seq=i,
            )
        )

    t0 = time.monotonic()
    async with app.run_test() as pilot:
        deadline = time.monotonic() + 5.0
        while not app._is_finished:  # noqa: SLF001
            if time.monotonic() > deadline:
                pytest.fail("TUI full lifecycle exceeded 5s")
            await pilot.pause(0.05)
        # 验证数据落地
        assert app._status_data.name == "e2e"  # noqa: SLF001
        assert app._status_data.state == "completed"  # noqa: SLF001
        assert "a1" in app._status_data.agents  # noqa: SLF001
        assert app._task_panel.data["T1"].status == "completed"  # noqa: SLF001
        assert len(app._msg_panel.data) == 1  # noqa: SLF001
        assert app._budget_data.used_tokens >= 100  # noqa: SLF001
        # 5 秒 DoD
        assert time.monotonic() - t0 < 5.0


@pytest.mark.asyncio
async def test_tui_sink_does_not_block_emit_under_load() -> None:
    """@brief TUI 满载时 emit 不应阻塞业务路径"""
    bus = ObservabilityBus()
    sink = TUISink(maxsize=10)
    bus.register_sink(sink)
    set_global_bus(bus)

    from agent_swarm.observability import emit

    # 灌 50 条——超过 sink maxsize
    t0 = time.monotonic()
    for i in range(50):
        await emit("task.created", "load-test", {"task_id": f"T{i}"})
    elapsed = time.monotonic() - t0
    # 50 条 emit < 1s（drop 是 O(1)，不阻塞）
    assert elapsed < 1.0
    # sink 累计 drop ≥ 40
    assert sink.dropped >= 40
    # queue 仍保持 maxsize
    assert sink.queue.qsize() == 10


def test_tui_cli_subcommand_exists() -> None:
    """@brief `agent-swarm tui --help` 不报错——证明 CLI 子命令注册成功"""
    from click.testing import CliRunner

    from agent_swarm.cli.main import cli

    runner = CliRunner()
    result = runner.invoke(cli, ["tui", "--help"])
    assert result.exit_code == 0
    assert "TUI" in result.output or "tui" in result.output.lower()
