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
    fake_provider: FakeLLMProvider,
) -> None:
    """B2 回归：runner 异常时 results 仍要 append 一行——保持 len(results) == len(tasks)"""

    # 让第一个任务的 LLM 调用抛异常
    class ExplodingProvider(FakeLLMProvider):
        async def chat(self, messages, **kwargs):  # type: ignore[override]
            self.calls.append(list(messages))
            if len(self.calls) == 1:
                raise RuntimeError("simulated provider crash")
            return await super().chat(messages, **kwargs)

    fake = ExplodingProvider()
    fake.script.append(ScriptedResponse(content="B ok", finish_reason="stop"))

    # B2 验证关键：第一个任务整体抛异常进入 swarm 的外层 except 分支
    # （而不是被 runner 内部 try/except 兜住），这样才会触发 results.append 的 stub 路径
    # 我们用 monkeypatch 直接替换 runner 的 LLMProvider
    from agent_swarm.core import swarm as swarm_mod

    def _get(_n, **_k):
        return fake

    import importlib

    importlib.reload(swarm_mod)  # 确保使用我们 patch 的 get_provider
    swarm_mod.get_provider = _get  # type: ignore[assignment]

    cfg = _two_task_cfg(tmp_path)
    s = swarm_mod.Swarm.from_dict(cfg, base_dir=tmp_path)
    result = await s.run()

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
