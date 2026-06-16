"""单元测试：AgentRunner 主循环——使用 fake_llm 验证 OTAR 行为"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_swarm.core.agent_runner import AgentRunner
from agent_swarm.core.types import Agent, AgentCapabilities, Task, ToolCall
from agent_swarm.tools.builtin.file_ops import ReadFileTool
from tests.conftest import FakeLLMProvider, ScriptedResponse


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "README.md").write_text("# project\nhello agent-swarm\n", encoding="utf-8")
    return tmp_path


def _make_agent(tools: set[str] | None = None) -> Agent:
    return Agent(
        id="a-1",
        role="reader",
        persona="answer concisely.",
        provider="openai",
        model="gpt-4o-mini",
        capabilities=AgentCapabilities.worker(tools or {"read_file"}),
        tools=list(tools or {"read_file"}),
        max_iterations=5,
    )


async def test_runner_single_step_no_tool(fake_llm: FakeLLMProvider, workspace: Path) -> None:
    """LLM 第一轮直接 stop——任务完成"""
    fake_llm.script.append(ScriptedResponse(content="quick answer", finish_reason="stop"))
    agent = _make_agent()
    runner = AgentRunner(agent, fake_llm, {"read_file": ReadFileTool(workspace)})
    task = Task(id="t-1", title="say hi", description="just respond")

    res = await runner.run(task)
    assert res.task.status == "completed"
    assert res.iterations == 1
    assert res.finish_reason == "stop"
    assert "quick answer" in res.final_text


async def test_runner_tool_then_stop(fake_llm: FakeLLMProvider, workspace: Path) -> None:
    """OTAR：第一轮调 read_file，第二轮 stop"""
    fake_llm.script.append(
        ScriptedResponse(
            tool_calls=[
                ToolCall(id="c1", name="read_file", arguments={"path": "README.md"})
            ],
            finish_reason="tool_use",
        )
    )
    fake_llm.script.append(
        ScriptedResponse(content="readme says hello", finish_reason="stop")
    )
    agent = _make_agent()
    runner = AgentRunner(agent, fake_llm, {"read_file": ReadFileTool(workspace)})
    task = Task(id="t-2", title="describe readme", description="read README and summarize")

    res = await runner.run(task)
    assert res.task.status == "completed"
    assert res.iterations == 2

    # 历史应包含：system / user / assistant(tool_call) / tool / assistant(stop) = 5 条
    roles = [t.role for t in res.history]
    assert roles == ["system", "user", "assistant", "tool", "assistant"]
    # tool 消息内容应包含 README 内的实际文字
    assert "agent-swarm" in res.history[3].content


async def test_runner_max_iterations(fake_llm: FakeLLMProvider, workspace: Path) -> None:
    """LLM 一直要求工具调用——达到 max_iterations 时被强制终止"""
    # 持续要求工具调用，直到测试结束
    for _ in range(10):
        fake_llm.script.append(
            ScriptedResponse(
                tool_calls=[
                    ToolCall(id=f"c{_}", name="read_file", arguments={"path": "README.md"})
                ],
                finish_reason="tool_use",
            )
        )
    agent = _make_agent()
    agent.max_iterations = 3
    runner = AgentRunner(agent, fake_llm, {"read_file": ReadFileTool(workspace)})
    task = Task(id="t-3", title="loopy", description="never stop")

    res = await runner.run(task)
    assert res.iterations == 3
    assert res.finish_reason == "max_iterations"
    # 任务被视为完成（W1 策略：避免 LLM 不收敛把整个 swarm 拖崩）
    assert res.task.status == "completed"


async def test_runner_unauthorized_tool(fake_llm: FakeLLMProvider, workspace: Path) -> None:
    """LLM 调用未授权工具——返回 [error] 而非崩溃"""
    fake_llm.script.append(
        ScriptedResponse(
            tool_calls=[
                ToolCall(id="c1", name="run_command", arguments={"cmd": "ls"})
            ],
            finish_reason="tool_use",
        )
    )
    fake_llm.script.append(ScriptedResponse(content="ok stopping", finish_reason="stop"))

    # capabilities 只授权 read_file；run_command 未授权
    agent = _make_agent(tools={"read_file"})
    runner = AgentRunner(
        agent,
        fake_llm,
        {"read_file": ReadFileTool(workspace)},  # tool 字典里也没有 run_command
    )
    task = Task(id="t-4", title="probe", description="x")
    res = await runner.run(task)

    # tool 消息记录了拒绝原因
    tool_turn = next(t for t in res.history if t.role == "tool")
    assert "[error]" in tool_turn.content
    assert "not available" in tool_turn.content


async def test_runner_capabilities_filter_tools(fake_llm: FakeLLMProvider, workspace: Path) -> None:
    """传给 LLM 的 tool schema 只含 capabilities 允许的工具"""
    fake_llm.script.append(ScriptedResponse(content="ok", finish_reason="stop"))
    # capabilities 只授权 read_file，但 tools 字典里塞两个
    agent = _make_agent(tools={"read_file"})
    extra_tool = ReadFileTool(workspace)
    extra_tool.name = "write_file"  # type: ignore[misc]  - 临时改名，模拟未授权工具
    runner = AgentRunner(
        agent,
        fake_llm,
        {
            "read_file": ReadFileTool(workspace),
            "write_file": extra_tool,
        },
    )
    task = Task(id="t-5", title="check", description="x")
    await runner.run(task)

    # 第一次 chat 调用时传入的 tools 列表应只含 read_file
    assert len(fake_llm.calls) == 1
    # 通过 tool_schemas 缓存检查（间接）：runner 内部仅含 read_file
    assert set(runner.tools.keys()) == {"read_file"}


async def test_runner_rejects_zero_max_iterations(
    fake_llm: FakeLLMProvider, workspace: Path
) -> None:
    """B1 回归：max_iterations=0 必须在 run 入口就拒绝，不能让 iteration 未定义崩在 return"""
    agent = _make_agent()
    agent.max_iterations = 0
    runner = AgentRunner(agent, fake_llm, {"read_file": ReadFileTool(workspace)})
    task = Task(id="t-zero", title="x", description="y")

    with pytest.raises(ValueError, match="max_iterations must be >= 1"):
        await runner.run(task)


async def test_runner_rejects_negative_max_iterations(
    fake_llm: FakeLLMProvider, workspace: Path
) -> None:
    """B1 回归：负数同样拒绝"""
    agent = _make_agent()
    agent.max_iterations = -1
    runner = AgentRunner(agent, fake_llm, {"read_file": ReadFileTool(workspace)})
    task = Task(id="t-neg", title="x", description="y")
    with pytest.raises(ValueError, match="max_iterations"):
        await runner.run(task)


async def test_runner_finish_reason_length_terminates(
    fake_llm: FakeLLMProvider, workspace: Path
) -> None:
    """LLM 因 max_tokens 截断——视为完成，不再继续 OTAR"""
    fake_llm.script.append(
        ScriptedResponse(
            content="partial answer",
            tool_calls=[ToolCall(id="c1", name="read_file", arguments={"path": "x"})],
            finish_reason="length",
        )
    )
    # 兜底：若误进下一轮，会取到这条
    fake_llm.script.append(ScriptedResponse(content="should not run", finish_reason="stop"))

    agent = _make_agent()
    runner = AgentRunner(agent, fake_llm, {"read_file": ReadFileTool(workspace)})
    res = await runner.run(Task(id="t-len", title="x", description="y"))

    assert res.finish_reason == "length"
    assert res.task.status == "completed"
    assert "partial answer" in res.final_text
    # 只调一次 LLM——不进 act
    assert len(fake_llm.calls) == 1


async def test_runner_llm_exception_marks_task_failed(
    workspace: Path,
) -> None:
    """LLM provider 抛异常——任务标记 failed，finish_reason='error'"""

    class BoomProvider(FakeLLMProvider):
        async def chat(self, messages, **kwargs):  # type: ignore[override]
            self.calls.append(list(messages))
            raise RuntimeError("api down")

    boom = BoomProvider()
    agent = _make_agent()
    runner = AgentRunner(agent, boom, {"read_file": ReadFileTool(workspace)})
    res = await runner.run(Task(id="t-err", title="x", description="y"))

    assert res.finish_reason == "error"
    assert res.task.status == "failed"
    assert res.task.error is not None
    assert "api down" in res.task.error


async def test_runner_initial_inbox_messages_appear_in_prompt(
    fake_llm: FakeLLMProvider, workspace: Path
) -> None:
    """W2: 启动时已有的 inbox 消息应渲染到首轮 user prompt"""
    from agent_swarm.core.types import Message

    fake_llm.script.append(ScriptedResponse(content="ok", finish_reason="stop"))

    agent = _make_agent()
    runner = AgentRunner(agent, fake_llm, {"read_file": ReadFileTool(workspace)})
    task = Task(id="t-msg", title="x", description="y")

    inbox = [
        Message(
            id="m-1",
            from_agent="other",
            to_agent="a-1",
            target_type="internal",
            msg_type="delegate",
            content="please handle X",
            timestamp=0.0,
        )
    ]
    await runner.run(task, inbox_messages=inbox)

    # 第一次 LLM 调用的 user turn 应含消息内容
    user_turn = next(t for t in fake_llm.calls[0] if t.role == "user")
    assert "please handle X" in user_turn.content
    assert "delegate" in user_turn.content
    assert "from other" in user_turn.content


async def test_runner_does_not_mutate_input_task(
    fake_llm: FakeLLMProvider, workspace: Path
) -> None:
    """
    W2-B2 回归：runner.run() 必须深拷贝 task，不污染入参对象

    没有这个保证，TaskQueue 持有的 task 会被 runner 直接改写状态/result，
    破坏 "TaskQueue 是任务状态唯一权威" 的契约。
    """
    fake_llm.script.append(ScriptedResponse(content="output", finish_reason="stop"))
    agent = _make_agent()
    runner = AgentRunner(agent, fake_llm, {"read_file": ReadFileTool(workspace)})

    original = Task(id="t-pure", title="x", description="y")
    assert original.status == "pending"
    assert original.assigned_to is None
    assert original.result is None

    res = await runner.run(original)

    # 入参对象保持不变——pending / 无 assigned_to / 无 result
    assert original.status == "pending"
    assert original.assigned_to is None
    assert original.result is None

    # 返回的 res.task 是副本，含最终状态
    assert res.task is not original
    assert res.task.status == "completed"
    assert res.task.assigned_to == "a-1"
    assert res.task.result == "output"


async def test_runner_does_not_mutate_input_task_on_failure(
    workspace: Path,
) -> None:
    """W2-B2 回归：LLM 失败也不污染入参 task"""

    class BoomProvider(FakeLLMProvider):
        async def chat(self, messages, **kwargs):  # type: ignore[override]
            self.calls.append(list(messages))
            raise RuntimeError("api down")

    boom = BoomProvider()
    agent = _make_agent()
    runner = AgentRunner(agent, boom, {"read_file": ReadFileTool(workspace)})

    original = Task(id="t-fail", title="x", description="y")
    res = await runner.run(original)

    # 原对象未动
    assert original.status == "pending"
    assert original.error is None
    # 副本含失败信息
    assert res.task.status == "failed"
    assert "api down" in (res.task.error or "")
