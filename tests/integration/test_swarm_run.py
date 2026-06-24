"""
@module tests.integration.test_swarm_run
@brief  Swarm.run() 集成测试——验证多任务串行 + 失败兜底（B2 修复）

层级定位（DESIGN.md §17.4 测试金字塔）:
  - 不是 unit：跨 Swarm + AgentRunner + Provider + Tool 多模块
  - 不是 e2e：不调真实 LLM（用 FakeLLMProvider）
  - 是 integration：mock LLM，真实其他组件
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_swarm.core.swarm import Swarm
from agent_swarm.core.types import ToolCall
from tests.conftest import FakeLLMProvider, ScriptedResponse


@pytest.fixture
def fake_provider(monkeypatch: pytest.MonkeyPatch) -> FakeLLMProvider:
    """注入 FakeLLMProvider 替换真实 OpenAI——全局生效"""
    fake = FakeLLMProvider()

    def _fake_get_provider(name: str, **kwargs):  # noqa: ARG001
        return fake

    monkeypatch.setattr("agent_swarm.core.swarm.get_provider", _fake_get_provider)
    return fake


def _two_task_cfg(tmp_path: Path) -> dict:
    return {
        "name": "two-task",
        "agents": [
            {
                "id": "a1",
                "role": "r",
                "persona": "p",
                "provider": "openai",
                "model": "gpt-4o-mini",
                "tools": ["read_file"],
                "max_iterations": 3,
            }
        ],
        "tasks": [
            {"title": "task A", "description": "first"},
            {"title": "task B", "description": "second"},
        ],
        "workspace": str(tmp_path),
    }


async def test_swarm_run_two_tasks_serial(
    tmp_path: Path,
    fake_provider: FakeLLMProvider,
) -> None:
    """两个任务串行跑通——验证 results 顺序与 task 顺序一致"""
    fake_provider.script.append(ScriptedResponse(content="A done", finish_reason="stop"))
    fake_provider.script.append(ScriptedResponse(content="B done", finish_reason="stop"))

    swarm = Swarm.from_dict(_two_task_cfg(tmp_path), base_dir=tmp_path)
    result = await swarm.run()

    assert result.state == "completed"
    assert result.tasks_completed == 2
    assert result.tasks_failed == 0
    assert len(result.agent_results) == 2
    assert result.agent_results[0].task.title == "task A"
    assert result.agent_results[1].task.title == "task B"


async def test_swarm_run_failed_task_still_appended(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    第一个任务的 LLM 抛异常 → runner.run 内部 try/except 把任务标 failed
    → run_loop 调 task_queue.fail → 第二个任务正常完成

    断言：失败 + 成功的 task_results 各占一条（保持 len 与 tasks 一致）
    """

    # 让第一个任务的 LLM 调用抛异常，第二个正常
    class ExplodingProvider(FakeLLMProvider):
        async def chat(self, messages, **kwargs):  # type: ignore[override]
            self.calls.append(list(messages))
            if len(self.calls) == 1:
                raise RuntimeError("simulated provider crash")
            return await super().chat(messages, **kwargs)

    fake = ExplodingProvider()
    fake.script.append(ScriptedResponse(content="B ok", finish_reason="stop"))

    def _get(name: str, **kwargs):  # noqa: ARG001
        return fake

    # 用标准 monkeypatch 注入；不要 importlib.reload（会破坏其他测试的引用）
    monkeypatch.setattr("agent_swarm.core.swarm.get_provider", _get)

    cfg = _two_task_cfg(tmp_path)
    swarm = Swarm.from_dict(cfg, base_dir=tmp_path)
    result = await swarm.run()

    # 即使第一个任务崩溃，agent_results 仍然有 2 条（B2 修复点）
    assert len(result.agent_results) == 2
    # 第一条任务 status=failed
    assert result.agent_results[0].task.status == "failed"
    assert result.agent_results[0].finish_reason in ("error", "stop")
    # 第二条任务 status=completed
    assert result.agent_results[1].task.status == "completed"
    # swarm 整体失败
    assert result.state == "failed"
    assert result.tasks_completed == 1
    assert result.tasks_failed == 1


async def test_swarm_run_tool_call_then_stop(
    tmp_path: Path,
    fake_provider: FakeLLMProvider,
) -> None:
    """完整 OTAR 集成：read_file 工具实际被调用且结果回灌到 LLM"""
    (tmp_path / "data.txt").write_text("payload-xyz", encoding="utf-8")

    fake_provider.script.append(
        ScriptedResponse(
            tool_calls=[ToolCall(id="c1", name="read_file", arguments={"path": "data.txt"})],
            finish_reason="tool_use",
        )
    )
    fake_provider.script.append(ScriptedResponse(content="saw payload-xyz", finish_reason="stop"))

    cfg = _two_task_cfg(tmp_path)
    cfg["tasks"] = [{"title": "read data", "description": "read data.txt"}]
    swarm = Swarm.from_dict(cfg, base_dir=tmp_path)
    result = await swarm.run()

    assert result.state == "completed"
    # 第二次 LLM 调用必须含 tool 角色的工具结果
    assert len(fake_provider.calls) == 2
    second_call_roles = [t.role for t in fake_provider.calls[1]]
    assert "tool" in second_call_roles
    # 工具结果应包含真实文件内容
    tool_turn = next(t for t in fake_provider.calls[1] if t.role == "tool")
    assert "payload-xyz" in tool_turn.content


async def test_swarm_run_called_twice_raises(
    tmp_path: Path,
    fake_provider: FakeLLMProvider,
) -> None:
    """W2-B8 回归：同一 Swarm 实例 run() 调用两次应抛 RuntimeError"""
    fake_provider.script.append(ScriptedResponse(content="ok", finish_reason="stop"))
    swarm = Swarm.from_dict(_two_task_cfg(tmp_path), base_dir=tmp_path)
    cfg_one_task = _two_task_cfg(tmp_path)
    cfg_one_task["tasks"] = [{"title": "only one"}]
    swarm = Swarm.from_dict(cfg_one_task, base_dir=tmp_path)

    res1 = await swarm.run()
    assert res1.state == "completed"

    with pytest.raises(RuntimeError, match="already called"):
        await swarm.run()


async def test_swarm_result_distinguishes_failed_vs_unfinished(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W2-B6 回归：tasks_failed 和 tasks_unfinished 分开统计"""

    # 让所有 LLM 调用直接抛——第一个任务变 failed
    class BoomProvider(FakeLLMProvider):
        async def chat(self, messages, **kwargs):  # type: ignore[override]
            self.calls.append(list(messages))
            raise RuntimeError("api down")

    boom = BoomProvider()

    def _get(name: str, **kwargs):  # noqa: ARG001
        return boom

    monkeypatch.setattr("agent_swarm.core.swarm.get_provider", _get)

    cfg = {
        "name": "fail-and-block",
        "agents": [
            {
                "id": "a",
                "role": "r",
                "persona": "p",
                "provider": "openai",
                "model": "gpt-4o-mini",
                "tools": [],
                "max_iterations": 2,
            }
        ],
        "tasks": [
            {"id": "T1", "title": "will-fail"},
            # T2 依赖 T1——T1 失败后 T2 永远 blocked
            {"id": "T2", "title": "blocked-forever", "depends_on": ["T1"]},
        ],
    }
    swarm = Swarm.from_dict(cfg, base_dir=tmp_path)
    res = await swarm.run()

    assert res.state == "failed"
    assert res.tasks_completed == 0
    assert res.tasks_failed == 1  # T1 真正失败
    assert res.tasks_unfinished == 1  # T2 卡在 blocked
    assert res.error is not None
