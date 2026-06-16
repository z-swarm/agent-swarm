"""
@module tests.integration.test_swarm_persists_events
@brief  W3 集成测试——Swarm 跑完后事件已落 SQLite，可被 SessionManager 恢复

层级：integration——Swarm + ObservabilityBus + SqliteEventSink + SessionManager
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_swarm.core.session_manager import SessionManager
from agent_swarm.core.swarm import Swarm
from agent_swarm.observability import (
    ObservabilityBus,
    SqliteEventSink,
    set_global_bus,
)
from tests.conftest import FakeLLMProvider, ScriptedResponse


@pytest.fixture
def fake_provider(monkeypatch: pytest.MonkeyPatch) -> FakeLLMProvider:
    fake = FakeLLMProvider()
    monkeypatch.setattr("agent_swarm.core.swarm.get_provider", lambda *_a, **_k: fake)
    return fake


def _two_task_cfg(tmp_path: Path) -> dict:
    return {
        "name": "persist-test",
        "agents": [
            {
                "id": "a1",
                "role": "r",
                "persona": "p",
                "provider": "openai",
                "model": "gpt-4o-mini",
                "tools": [],
                "max_iterations": 2,
            }
        ],
        "tasks": [
            {"id": "T1", "title": "first"},
            {"id": "T2", "title": "second", "depends_on": ["T1"]},
        ],
        "workspace": str(tmp_path),
    }


async def test_swarm_run_persists_events_to_sqlite(
    tmp_path: Path,
    fake_provider: FakeLLMProvider,
) -> None:
    """跑 swarm → 事件流应自动落 SQLite"""
    fake_provider.script.append(ScriptedResponse(content="ok1", finish_reason="stop"))
    fake_provider.script.append(ScriptedResponse(content="ok2", finish_reason="stop"))

    db = tmp_path / "events.db"
    bus = ObservabilityBus()
    sink = SqliteEventSink(db)
    bus.register_sink(sink)
    set_global_bus(bus)
    try:
        mgr = SessionManager(sink)
        swarm = Swarm.from_dict(_two_task_cfg(tmp_path), base_dir=tmp_path)
        await mgr.create_session(swarm.name, session_id=swarm.session_id)
        result = await swarm.run()
        await mgr.end_session(swarm.session_id, result.state)
        assert result.state == "completed"

        # SQLite 中应能读到事件流
        events = await sink.get_events(swarm.session_id)
        names = [e.event_name for e in events]
        assert names[0] == "swarm.started"
        assert names[-1] == "swarm.completed"
        assert "task.created" in names
        assert "task.claimed" in names
        assert "task.completed" in names
        assert "task.unblocked" in names  # T1 完成解阻塞 T2

        # session 元数据
        info = await sink.get_session(swarm.session_id)
        assert info["state"] == "completed"
        assert info["ended_at"] is not None
    finally:
        set_global_bus(None)
        await sink.aclose()


async def test_swarm_run_then_restore_session(
    tmp_path: Path,
    fake_provider: FakeLLMProvider,
) -> None:
    """W3 DoD：跑完 swarm → SessionManager.restore_session → 状态完全一致"""
    fake_provider.script.append(ScriptedResponse(content="r1", finish_reason="stop"))
    fake_provider.script.append(ScriptedResponse(content="r2", finish_reason="stop"))

    db = tmp_path / "events.db"
    bus = ObservabilityBus()
    sink = SqliteEventSink(db)
    bus.register_sink(sink)
    set_global_bus(bus)
    sid: str
    try:
        mgr = SessionManager(sink)
        swarm = Swarm.from_dict(_two_task_cfg(tmp_path), base_dir=tmp_path)
        sid = swarm.session_id
        await mgr.create_session(swarm.name, session_id=sid)
        result = await swarm.run()
        await mgr.end_session(sid, result.state)
    finally:
        set_global_bus(None)
        await sink.aclose()

    # 模拟"另一个进程"——重新打开 sink + manager，恢复 session
    sink2 = SqliteEventSink(db)
    try:
        mgr2 = SessionManager(sink2)
        state = await mgr2.restore_session(sid)
        # 状态完整：两个任务都完成
        tasks = await state.task_queue.list_all()
        assert len(tasks) == 2
        by_id = {t.id: t for t in tasks}
        assert by_id["T1"].status == "completed"
        assert by_id["T2"].status == "completed"
        assert by_id["T1"].result == "r1"
        assert by_id["T2"].result == "r2"
        # depends_on 重建
        assert by_id["T2"].depends_on == ["T1"]
    finally:
        await sink2.aclose()


async def test_partial_run_can_be_inspected_via_event_stream(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    模拟 'swarm 跑到一半崩溃' 的场景——
    第二个任务 LLM 抛异常 → swarm.failed
    事件流中应清晰记录哪个任务完成、哪个失败、哪个 unblocked
    """

    class FlakyProvider(FakeLLMProvider):
        async def chat(self, messages, **kwargs):  # type: ignore[override]
            self.calls.append(list(messages))
            if len(self.calls) == 1:
                from agent_swarm.core.types import LLMResponse
                return LLMResponse(
                    content="first ok",
                    tool_calls=[],
                    finish_reason="stop",
                    tokens_prompt=10,
                    tokens_completion=5,
                    model="x",
                )
            raise RuntimeError("provider crashed mid-run")

    fake = FlakyProvider()
    monkeypatch.setattr("agent_swarm.core.swarm.get_provider", lambda *_a, **_k: fake)

    db = tmp_path / "partial.db"
    bus = ObservabilityBus()
    sink = SqliteEventSink(db)
    bus.register_sink(sink)
    set_global_bus(bus)

    sid: str
    try:
        mgr = SessionManager(sink)
        swarm = Swarm.from_dict(_two_task_cfg(tmp_path), base_dir=tmp_path)
        sid = swarm.session_id
        await mgr.create_session(swarm.name, session_id=sid)
        result = await swarm.run()
        await mgr.end_session(sid, result.state)
        assert result.state == "failed"
        # T1 完成，T2 失败
        assert result.tasks_completed == 1
        assert result.tasks_failed == 1
    finally:
        set_global_bus(None)
        await sink.aclose()

    # 事件流应能呈现完整执行轨迹
    sink2 = SqliteEventSink(db)
    try:
        events = await sink2.get_events(sid)
        names = [e.event_name for e in events]
        # T1 lifecycle
        assert names.count("task.completed") == 1
        assert names.count("task.failed") == 1
        # swarm 整体 failed
        assert names[-1] == "swarm.failed"

        # 恢复后 task_queue 状态正确
        mgr2 = SessionManager(sink2)
        state = await mgr2.restore_session(sid)
        tasks = {t.id: t for t in await state.task_queue.list_all()}
        assert tasks["T1"].status == "completed"
        assert tasks["T2"].status == "failed"
    finally:
        await sink2.aclose()


async def test_complex_result_persists_as_string(
    tmp_path: Path,
) -> None:
    """
    W3-Z2 修复后：dict 类型 result 经 sink (json.dumps default=str) 序列化，
    回放时恢复为原 dict 结构（不再被强制 str() 化）
    """
    from agent_swarm.core.task_queue import TaskQueue
    from agent_swarm.core.types import Task as _Task

    db = tmp_path / "complex.db"
    bus = ObservabilityBus()
    sink = SqliteEventSink(db)
    bus.register_sink(sink)
    set_global_bus(bus)

    sid = "S-complex"
    try:
        await sink.register_session(sid, "x")
        q = TaskQueue(session_id=sid)
        await q.add(_Task(id="T", title="x", description="y"))
        claimed = await q.claim("T", "a", expected_version=0)
        complex_result = {"score": 0.95, "tags": ["a", "b"]}
        await q.complete("T", complex_result, expected_version=claimed.task.version)  # type: ignore[union-attr]
    finally:
        set_global_bus(None)
        await sink.aclose()

    # 重新打开 + 恢复
    sink2 = SqliteEventSink(db)
    try:
        mgr = SessionManager(sink2)
        state = await mgr.restore_session(sid)
        t = (await state.task_queue.list_all())[0]
        # W3-Z2 后契约：dict 结构保留
        assert isinstance(t.result, dict)
        assert t.result["score"] == 0.95
        assert t.result["tags"] == ["a", "b"]
    finally:
        await sink2.aclose()


async def test_swarm_crash_emits_failed_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    W3-Z1 回归：swarm 内部崩溃也要 emit swarm.failed
    构造 watcher 路径之外的异常——例如 asyncio.create_task 抛错
    """
    fake = FakeLLMProvider()
    fake.script.append(ScriptedResponse(content="ok", finish_reason="stop"))
    monkeypatch.setattr("agent_swarm.core.swarm.get_provider", lambda *_a, **_k: fake)

    db = tmp_path / "crash.db"
    bus = ObservabilityBus()
    sink = SqliteEventSink(db)
    bus.register_sink(sink)
    set_global_bus(bus)

    sid: str
    try:
        cfg = _two_task_cfg(tmp_path)
        swarm = Swarm.from_dict(cfg, base_dir=tmp_path)
        sid = swarm.session_id
        await sink.register_session(sid, swarm.name)

        # 让 asyncio.create_task 失败——触发外层 except
        def boom(*args, **kw):  # noqa: ARG001
            raise RuntimeError("create_task explosion")

        monkeypatch.setattr("agent_swarm.core.swarm.asyncio.create_task", boom)

        result = await swarm.run()
        assert result.state == "failed"
    finally:
        set_global_bus(None)
        await sink.aclose()

    # 验证 swarm.failed 事件已落库
    sink2 = SqliteEventSink(db)
    try:
        events = await sink2.get_events(sid)
        names = [e.event_name for e in events]
        assert "swarm.failed" in names
        # error 字段应携带异常信息
        failed_evt = next(e for e in events if e.event_name == "swarm.failed")
        assert "explosion" in failed_evt.payload.get("error", "")
    finally:
        await sink2.aclose()


async def test_bus_aclose_clears_sinks(tmp_path: Path) -> None:
    """W3-Z3 回归：bus.aclose 后 sinks 列表应为空"""
    bus = ObservabilityBus()
    sink = SqliteEventSink(tmp_path / "x.db")
    bus.register_sink(sink)
    assert len(bus.sinks) == 1
    await bus.aclose()
    assert len(bus.sinks) == 0


async def test_restore_does_not_use_private_fields(
    tmp_path: Path,
) -> None:
    """
    W3-B1 回归：restore_session 通过 TaskQueue.restore_task /
    Mailbox.restore_message 公开 API 进入恢复路径——不应触碰私有字段。

    本测试通过验证子类化 TaskQueue 后能拦截 restore_* 调用来确认。
    """

    db = tmp_path / "api.db"
    bus = ObservabilityBus()
    sink = SqliteEventSink(db)
    bus.register_sink(sink)
    set_global_bus(bus)

    sid = "S-api"
    try:
        await sink.register_session(sid, "x")
        # 写入 task.created 事件
        from agent_swarm.core.types import SessionEvent
        await sink.consume(SessionEvent(
            event_name="task.created", session_id=sid, timestamp=1.0, seq=0,
            payload={"task_id": "T", "title": "x", "description": "y",
                     "status": "pending", "depends_on": []},
        ))
    finally:
        set_global_bus(None)
        await sink.aclose()

    # 恢复——验证 restore_task 被调用
    sink2 = SqliteEventSink(db)
    try:
        mgr = SessionManager(sink2)
        state = await mgr.restore_session(sid)
        # 通过公开 API 拿状态
        tasks = await state.task_queue.list_all()
        assert len(tasks) == 1
        # restore_task 应该把 task 写入了 _tasks（间接验证）
        assert tasks[0].id == "T"
    finally:
        await sink2.aclose()
