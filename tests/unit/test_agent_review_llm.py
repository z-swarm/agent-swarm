"""
@module tests.unit.test_agent_review_llm
@brief  P5-W37 真实 LLM judge 接入单测 (≥15 cases)

覆盖:
  - _openai_judge_fn parse response (mock SDK) → Judgement
  - _openai_judge_fn SDK error → UNCERTAIN 兜底
  - _anthropic_judge_fn parse response (mock SDK) → Judgement
  - _anthropic_judge_fn SDK error → UNCERTAIN 兜底
  - run_full_review 真实流程 (mock judge_fn) → 含 1 finding
  - run_full_review 缺 API key → fail-fast
  - run_full_review 无 findings → approve
  - llm_judge_factory 3 provider 真实返回
  - review_runner 接入真实 judge_fn
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# tools/ 在 PYTHONPATH 外, 加进 sys.path 才能 import agent_review
_TOOLS_DIR = Path(__file__).resolve().parents[2] / "tools"
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

from agent_swarm.core.types import Judgement, Stance  # noqa: E402


@pytest.fixture(autouse=True)  # noqa: E402
def _reset_agent_review_module():
    """
    @brief W37: 重置 agent_review 模块的 module-level REPO 状态

    @note  REPO 在 import 时固定 (env read once), 后续测试改 env 无效
    @note  重置 REPO + sys.modules, 让每个 test 重新读 env
    """
    yield
    sys.modules.pop("agent_review", None)
    if "agent_review" in sys.modules:
        del sys.modules["agent_review"]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


@dataclass
class _FakeAgent:
    id: str = "judge-0"
    model: str = "gpt-4o-mini"
    provider: str = "openai"


@dataclass
class _FakeOpenAIResp:
    """模拟 OpenAI ChatCompletion response"""

    content_str: str = '{"stance": "support", "confidence": 0.9, "reasoning": "real issue", "evidence": ["app.py:42"]}'


@dataclass
class _FakeChoice:
    message: _FakeOpenAIMessage = field(default_factory=lambda: _FakeOpenAIMessage())


@dataclass
class _FakeOpenAIMessage:
    content: str = '{"stance": "support", "confidence": 0.9, "reasoning": "real issue", "evidence": ["app.py:42"]}'


@dataclass
class _FakeOpenAIResponse:
    choices: list = field(default_factory=lambda: [_FakeChoice()])


@dataclass
class _FakeAnthropicText:
    text: str = (
        '{"stance": "refute", "confidence": 0.8, "reasoning": "false positive", "evidence": []}'
    )


@dataclass
class _FakeAnthropicResponse:
    content: list = field(default_factory=lambda: [_FakeAnthropicText()])


def _make_git_repo(path: Path) -> Path:
    """构造带 secret leak 的 git repo (触发 static scan finding)"""
    path.mkdir(parents=True, exist_ok=True)
    for cmd in [
        ["git", "init", "-b", "main"],
        ["git", "config", "user.name", "Test"],
        ["git", "config", "user.email", "t@e.com"],
        ["git", "config", "commit.gpgsign", "false"],
    ]:
        subprocess.run(cmd, cwd=str(path), capture_output=True, text=True, timeout=15, check=True)
    (path / "app.py").write_text("# app\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "app.py"],
        cwd=str(path),
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=str(path),
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    (path / "app.py").write_text(
        '# app\nAPI_KEY = "sk-1234567890abcdefghijklmnopqrstuvwxyz1234567890abcd"\n',
        encoding="utf-8",
    )
    subprocess.run(
        ["git", "add", "app.py"],
        cwd=str(path),
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "add api key"],
        cwd=str(path),
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    return path


# ---------------------------------------------------------------------------
# 1. _openai_judge_fn
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_openai_judge_parse_support() -> None:
    """openai judge 解析 SUPPORT stance"""
    with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}):
        from agent_review import _openai_judge_fn

        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(return_value=_FakeOpenAIResponse())
        with patch("openai.AsyncOpenAI", return_value=mock_client):
            agent = _FakeAgent()
            j = await _openai_judge_fn(agent, "h0", 1)
    assert isinstance(j, Judgement)
    assert j.stance == Stance.SUPPORT
    assert j.confidence == 0.9
    assert "real issue" in j.reasoning
    assert j.evidence == ["app.py:42"]


@pytest.mark.asyncio
async def test_openai_judge_parse_refute() -> None:
    """openai judge 解析 REFUTE stance"""
    msg = _FakeOpenAIMessage(
        content='{"stance": "refute", "confidence": 0.7, "reasoning": "no", "evidence": []}'
    )
    resp = _FakeOpenAIResponse(choices=[_FakeChoice(message=msg)])
    with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}):
        from agent_review import _openai_judge_fn

        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(return_value=resp)
        with patch("openai.AsyncOpenAI", return_value=mock_client):
            j = await _openai_judge_fn(_FakeAgent(), "h0", 1)
    assert j.stance == Stance.REFUTE
    assert j.confidence == 0.7


@pytest.mark.asyncio
async def test_openai_judge_sdk_error_fallback_uncertain() -> None:
    """openai SDK error → UNCERTAIN 兜底"""
    with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}):
        from agent_review import _openai_judge_fn

        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(side_effect=RuntimeError("SDK error"))
        with patch("openai.AsyncOpenAI", return_value=mock_client):
            j = await _openai_judge_fn(_FakeAgent(), "h0", 1)
    assert j.stance == Stance.UNCERTAIN
    assert "judge_fn error" in j.reasoning


@pytest.mark.asyncio
async def test_openai_judge_no_key_raises() -> None:
    """openai 无 API key 抛 RuntimeError"""
    with patch.dict(os.environ, {}, clear=True):
        from agent_review import _openai_judge_fn

        with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
            await _openai_judge_fn(_FakeAgent(), "h0", 1)


# ---------------------------------------------------------------------------
# 2. _anthropic_judge_fn
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_anthropic_judge_parse_refute() -> None:
    """anthropic judge 解析 REFUTE stance"""
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        from agent_review import _anthropic_judge_fn

        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=_FakeAnthropicResponse())
        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            agent = _FakeAgent(provider="anthropic", model="claude-3-5-sonnet-20241022")
            j = await _anthropic_judge_fn(agent, "h0", 1)
    assert isinstance(j, Judgement)
    assert j.stance == Stance.REFUTE
    assert j.confidence == 0.8


@pytest.mark.asyncio
async def test_anthropic_judge_parse_code_block() -> None:
    """anthropic 返 ```json ... ``` 包裹也能解析"""
    text = _FakeAnthropicText(
        text='```json\n{"stance": "support", "confidence": 0.85, "reasoning": "ok", "evidence": []}\n```'
    )
    resp = _FakeAnthropicResponse(content=[text])
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        from agent_review import _anthropic_judge_fn

        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=resp)
        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            j = await _anthropic_judge_fn(_FakeAgent(provider="anthropic"), "h0", 1)
    assert j.stance == Stance.SUPPORT
    assert j.confidence == 0.85


@pytest.mark.asyncio
async def test_anthropic_judge_sdk_error_fallback() -> None:
    """anthropic SDK error → UNCERTAIN 兜底"""
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        from agent_review import _anthropic_judge_fn

        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(side_effect=RuntimeError("timeout"))
        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            j = await _anthropic_judge_fn(_FakeAgent(provider="anthropic"), "h0", 1)
    assert j.stance == Stance.UNCERTAIN


@pytest.mark.asyncio
async def test_anthropic_judge_no_key_raises() -> None:
    """anthropic 无 API key 抛 RuntimeError"""
    with patch.dict(os.environ, {}, clear=True):
        from agent_review import _anthropic_judge_fn

        with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
            await _anthropic_judge_fn(_FakeAgent(provider="anthropic"), "h0", 1)


# ---------------------------------------------------------------------------
# 3. run_full_review 真实流程
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_full_review_openai_real_flow(tmp_path: Path) -> None:
    """run_full_review openai 真实流程 (mock SDK) → 流程跑通 + SDK 被调 + finding 存在"""
    repo = _make_git_repo(tmp_path / "llm-repo")
    with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key", "AGENT_REVIEW_REPO": str(repo)}):
        from agent_review import run_full_review

        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(return_value=_FakeOpenAIResponse())
        with patch("openai.AsyncOpenAI", return_value=mock_client):
            report = await run_full_review("HEAD~1..HEAD", llm_provider="openai")
    # 流程跑通 + 至少 1 finding (static scan 捕到 secret_leak)
    assert len(report.findings) >= 1
    # SDK 被调 (3 judge × N rounds)
    assert mock_client.chat.completions.create.called
    # summary 应反映 verifier 跑了
    assert "verifier" in report.summary or "rounds" in report.summary


@pytest.mark.asyncio
async def test_run_full_review_openai_no_key_raises(tmp_path: Path) -> None:
    """run_full_review openai 无 key fail-fast"""
    repo = _make_git_repo(tmp_path / "no-key-repo")
    with patch.dict(os.environ, {"AGENT_REVIEW_REPO": str(repo)}, clear=True):
        from agent_review import run_full_review

        with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
            await run_full_review("HEAD~1..HEAD", llm_provider="openai")


@pytest.mark.asyncio
async def test_run_full_review_anthropic_real_flow(tmp_path: Path) -> None:
    """run_full_review anthropic 真实流程 (mock SDK) → 流程跑通 + SDK 被调"""
    repo = _make_git_repo(tmp_path / "anthro-repo")
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key", "AGENT_REVIEW_REPO": str(repo)}):
        from agent_review import run_full_review

        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=_FakeAnthropicResponse())
        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            report = await run_full_review("HEAD~1..HEAD", llm_provider="anthropic")
    assert len(report.findings) >= 1
    assert mock_client.messages.create.called
    assert "verifier" in report.summary or "rounds" in report.summary


@pytest.mark.asyncio
async def test_run_full_review_fake_no_findings_returns_approve(tmp_path: Path) -> None:
    """run_full_review fake + 无 findings → approve"""
    repo = _make_git_repo(tmp_path / "clean-repo")
    # 重写第二个 commit 为干净 PR
    (repo / "app.py").write_text("# app\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "app.py"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "revert"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    with patch.dict(os.environ, {"AGENT_REVIEW_REPO": str(repo)}):
        from agent_review import run_full_review

        report = await run_full_review("HEAD~1..HEAD", llm_provider="fake")
    assert report.verdict == "approve"
    assert report.findings == []


@pytest.mark.asyncio
async def test_run_full_review_verifier_failure_fallback(tmp_path: Path) -> None:
    """run_full_review verifier 失败 → fallback findings"""
    repo = _make_git_repo(tmp_path / "fail-repo")
    with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key", "AGENT_REVIEW_REPO": str(repo)}):
        from agent_review import run_full_review

        # mock SDK 全部抛错 → judge_fn 全部 UNCERTAIN → verifier 2 轮 all_failed → 抛 VerifierStallError
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(side_effect=RuntimeError("network"))
        with patch("openai.AsyncOpenAI", return_value=mock_client):
            report = await run_full_review("HEAD~1..HEAD", llm_provider="openai")
    # 流程跑通, 至少 1 finding 保留 (fallback 保留 static scan 结果)
    assert len(report.findings) >= 1
    assert mock_client.chat.completions.create.called


# ---------------------------------------------------------------------------
# 4. llm_judge_factory 升级
# ---------------------------------------------------------------------------


def test_llm_factory_fake_returns_deterministic() -> None:
    """fake provider 返 _deterministic_judge"""
    from agent_swarm.web import review_runner

    judge = review_runner.llm_judge_factory("fake")
    assert callable(judge)
    assert judge.__name__ == "_deterministic_judge"


def test_llm_factory_openai_returns_real_judge() -> None:
    """openai provider 返 _openai_judge_fn (W37 真实接入)"""
    with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}):
        from agent_swarm.web import review_runner

        judge = review_runner.llm_judge_factory("openai")
    assert judge.__name__ == "_openai_judge_fn"


def test_llm_factory_anthropic_returns_real_judge() -> None:
    """anthropic provider 返 _anthropic_judge_fn (W37 真实接入)"""
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        from agent_swarm.web import review_runner

        judge = review_runner.llm_judge_factory("anthropic")
    assert judge.__name__ == "_anthropic_judge_fn"


def test_llm_factory_openai_no_key_raises() -> None:
    """openai 无 key 抛 RuntimeError (W36f 兼容)"""
    with patch.dict(os.environ, {}, clear=True):
        from agent_swarm.web import review_runner

        with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
            review_runner.llm_judge_factory("openai")


# ---------------------------------------------------------------------------
# 5. 异步路径接入
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_full_review_async_uses_real_judge(tmp_path: Path) -> None:
    """run_full_review_async 在 openai 模式调真实 run_full_review (mock SDK)"""
    repo = _make_git_repo(tmp_path / "async-real-repo")
    from agent_swarm.web import review_runner

    review_runner._TASK_STORE.clear()
    review_runner._TASK_QUEUES.clear()
    with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key", "AGENT_REVIEW_REPO": str(repo)}):
        task = review_runner.create_task("HEAD~1..HEAD", "openai")
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(return_value=_FakeOpenAIResponse())
        with patch("openai.AsyncOpenAI", return_value=mock_client):
            await review_runner.run_full_review_async(
                task.task_id,
                "HEAD~1..HEAD",
                repo,
                "openai",
                timeout=10.0,
            )
    done = review_runner.get_task(task.task_id)
    assert done is not None
    assert done.status == "done"
    assert done.result is not None
    assert len(done.result.get("findings", [])) >= 1
    review_runner._TASK_STORE.clear()
    review_runner._TASK_QUEUES.clear()
