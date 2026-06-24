"""
@module agent_swarm.web.review_runner
@brief  P5-W36b/W36f — Web 与 tools/agent_review 之间的薄包装

职责:
  - W36b: 把 run_simple_review 的 cwd 切到指定 repo_root (同步路径)
  - W36f: 内存 task store + LLM judge factory + 异步 full review runner
  - 把 ReviewReport dataclass 序列化为 dict
  - 把异常 (非 git repo / 无 diff) 转成 RuntimeError 让 routes.py 处理

为什么不直接 import tools.agent_review?
  - tools/ 不在 PYTHONPATH 标准包路径下, import 不优雅
  - review_runner 是 web 模块的内部接口, 限定 import 边界
  - W36f full mode (LLM + 对抗式) 走异步时, 在 review_runner 内扩展

@note W36b 阶段只接 run_simple_review (W13 决策); 全模式 W36f 落地
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

log = logging.getLogger(__name__)

# 把 tools/ 加进 sys.path 一次性
_TOOLS_DIR = Path(__file__).resolve().parents[3] / "tools"
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))


def _is_git_repo(path: Path) -> bool:
    """
    @brief 检查 path 是否在 git 仓库内

    @param path 任意目录
    @return True = 是 git repo, False = 否
    @note  用 git rev-parse --is-inside-work-tree 判定 (标准方式)
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=str(path),
            capture_output=True,
            text=True,
            timeout=5,
        )
    except FileNotFoundError:
        return False
    except Exception:
        return False
    return result.returncode == 0 and result.stdout.strip() == "true"


def run_review_sync(
    pr_ref: str,
    repo_root: Path | None,
) -> dict[str, Any]:
    """
    @brief 同步跑 simple review, 返 dict (含完整 ReviewReport)

    @param pr_ref    形如 "main..HEAD" 或 "abc..def"
    @param repo_root git 仓库根 (None 时用 cwd)
    @return ReviewReport 序列化 dict
    @raise FileNotFoundError git 不在 PATH
    @raise RuntimeError       非 git repo / git 异常 / no diff

    @note agent_review 内部用 AGENT_REVIEW_REPO 环境变量定位仓库 (W13 设计),
          本函数通过设置/恢复该 env 让 review 跑在指定 repo_root。
    """
    cwd: str | None = None
    if repo_root is not None:
        if not repo_root.exists():
            raise RuntimeError(f"repo_root {repo_root!r} does not exist")
        cwd = str(repo_root)
    # 前置检查: cwd 必须是 git repo
    check_path = Path(cwd) if cwd else Path.cwd()
    if not _is_git_repo(check_path):
        raise RuntimeError(f"not a git repository: {check_path}")
    # 临时设 AGENT_REVIEW_REPO env (agent_review 在 import 时读此 env 定位仓库)
    # 必须在 import agent_review 之前设置, 不然 REPO 常量已固定
    old_env: str | None = os.environ.get("AGENT_REVIEW_REPO")
    if cwd is not None:
        os.environ["AGENT_REVIEW_REPO"] = cwd
    # 清空 sys.modules 中可能的缓存, 让 agent_review 重新 import
    sys.modules.pop("agent_review", None)
    try:
        # 延迟 import (避免 tools/ 加 path 时机问题 + 上面 env 必须先设)
        from agent_review import run_simple_review

        report = run_simple_review(pr_ref)
        return asdict(report)
    except Exception as exc:
        # 把 agent_review 的异常分类 (routes.py 区分处理)
        msg = str(exc).lower()
        if "not a git" in msg or "not a git repository" in msg:
            raise RuntimeError("not a git repository") from exc
        if "no such file" in msg or "no diff" in msg:
            raise RuntimeError(f"no diff: {exc}") from exc
        raise RuntimeError(f"agent_review failed: {exc}") from exc
    finally:
        # 恢复 env
        if old_env is None:
            os.environ.pop("AGENT_REVIEW_REPO", None)
        else:
            os.environ["AGENT_REVIEW_REPO"] = old_env
        # 清 sys.modules 让下次调用时根据 env 重新 import
        sys.modules.pop("agent_review", None)


__all__ = ["run_review_sync", "_is_git_repo"]


# ---------------------------------------------------------------------------
# P5-W36f: full mode (LLM + 对抗式) + 异步任务 + 内存 task store
# ---------------------------------------------------------------------------


# 复用 W36b 的 env hack (在 import agent_review 之前设 AGENT_REVIEW_REPO)
# 见 run_review_sync 的处理模式


@dataclass
class ReviewTask:
    """
    @brief W36f: 异步 review 任务状态

    @field task_id       任务 ID (uuid4 hex 32)
    @field status        pending / running / done / error
    @field progress      0-100 进度
    @field log           进度日志 (list[str])
    @field result        ReviewReport 序列化 dict (None 直到 done)
    @field error         错误信息 (None 直到 error)
    @field created_at    epoch 时间戳
    @field pr_ref        入参 pr_ref
    @field llm_provider  openai / anthropic / fake

    @note 单进程内存 store; 多 worker 留 W37+ (Redis / Postgres)
    """

    task_id: str
    status: Literal["pending", "running", "done", "error"] = "pending"
    progress: int = 0
    log: list[str] = field(default_factory=list)
    result: dict[str, Any] | None = None
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    pr_ref: str = "main..HEAD"
    llm_provider: str = "fake"


# 全局 task store (单进程, in-memory)
# key = task_id (uuid4 hex 32)
_TASK_STORE: dict[str, ReviewTask] = {}
# SSE 订阅者队列: task_id -> list[asyncio.Queue]
_TASK_QUEUES: dict[str, list[asyncio.Queue]] = {}
# task 过期时间 (秒): 完成后保留 1 小时供查; 超时清理
_TASK_TTL_SECONDS = 3600
_CLEANUP_INTERVAL = 600  # 10 min


def create_task(pr_ref: str, llm_provider: str) -> ReviewTask:
    """
    @brief W36f: 创建新 review task (status=pending)

    @param pr_ref       git diff range
    @param llm_provider openai / anthropic / fake
    @return ReviewTask 实例 (已加入 _TASK_STORE)
    """
    task_id = uuid.uuid4().hex
    task = ReviewTask(
        task_id=task_id,
        pr_ref=pr_ref,
        llm_provider=llm_provider,
    )
    _TASK_STORE[task_id] = task
    _TASK_QUEUES[task_id] = []
    return task


def get_task(task_id: str) -> ReviewTask | None:
    """W36f: 查 task 状态 (返 None = 不存在或已清理)"""
    return _TASK_STORE.get(task_id)


def subscribe_task(task_id: str) -> asyncio.Queue | None:
    """
    @brief W36f: 订阅 task 进度事件 (SSE 用)

    @param task_id 任务 ID
    @return asyncio.Queue (None = task 不存在)
    @note  每次订阅创建一个新 queue, 进度事件推给所有订阅者
    """
    task = _TASK_STORE.get(task_id)
    if task is None:
        return None
    q: asyncio.Queue = asyncio.Queue()
    _TASK_QUEUES.setdefault(task_id, []).append(q)
    return q


def _emit_event(task_id: str, event: dict[str, Any]) -> None:
    """
    @brief 内部: 推进度事件给所有订阅者

    @param task_id  任务 ID
    @param event    事件 dict (e.g. {"type": "progress", "progress": 30})
    @note  没人订阅时静默丢 (task 可能已完成)
    """
    for q in _TASK_QUEUES.get(task_id, []):
        with contextlib.suppress(asyncio.QueueFull):
            q.put_nowait(event)  # 满则丢 (消费者跟不上)


def _update_task(task_id: str, **kwargs: Any) -> None:
    """
    @brief 内部: 更新 task 字段 + 发事件

    @param task_id  任务 ID
    @param kwargs   字段更新 (status / progress / log / result / error)
    """
    task = _TASK_STORE.get(task_id)
    if task is None:
        return
    for k, v in kwargs.items():
        setattr(task, k, v)
    # 同步推 SSE 事件
    event: dict[str, Any] = {"type": "update", "task_id": task_id}
    if "status" in kwargs:
        event["status"] = kwargs["status"]
    if "progress" in kwargs:
        event["progress"] = kwargs["progress"]
    if "log" in kwargs and kwargs["log"]:
        # log 是 list, 取最后一条
        event["log"] = kwargs["log"][-1]
    if "result" in kwargs and kwargs["result"] is not None:
        event["result"] = kwargs["result"]
    if "error" in kwargs and kwargs["error"] is not None:
        event["error"] = kwargs["error"]
    _emit_event(task_id, event)


def cleanup_expired_tasks() -> int:
    """
    @brief 内部: 清理过期 task (done/error 超过 TTL)

    @return 清理数量
    @note  定期任务调用 (e.g. 后台 loop, 间隔 _CLEANUP_INTERVAL)
    """
    now = time.time()
    expired = [
        tid
        for tid, t in _TASK_STORE.items()
        if t.status in ("done", "error") and (now - t.created_at) > _TASK_TTL_SECONDS
    ]
    for tid in expired:
        _TASK_STORE.pop(tid, None)
        _TASK_QUEUES.pop(tid, None)
    return len(expired)


def llm_judge_factory(provider: str) -> Any:
    """
    @brief W36f: LLM judge 工厂 (openai / anthropic / fake)

    @param provider  openai / anthropic / fake
    @return JudgeFn (async callable: agent, hypothesis_id, round_no -> Judgement)
    @raise ValueError  未知 provider
    @raise RuntimeError  缺 API key (openai / anthropic)
    @note  fake 模式: 模拟 3 judge × N 假设, 返确定性 SUPPORT
    """
    if provider == "fake":
        # fake: 复用 agent_review 的 _deterministic_judge (W13 已实现)
        from agent_review import _deterministic_judge

        return _deterministic_judge
    if provider == "openai":
        if not os.environ.get("OPENAI_API_KEY"):
            raise RuntimeError("OPENAI_API_KEY not set; please set it or use --web-review-llm fake")
        # W37: 真实 LLM judge, 调 OpenAI SDK
        from agent_review import _openai_judge_fn

        return _openai_judge_fn
    if provider == "anthropic":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError(
                "ANTHROPIC_API_KEY not set; please set it or use --web-review-llm fake"
            )
        # W37: 真实 LLM judge, 调 Anthropic SDK
        from agent_review import _anthropic_judge_fn

        return _anthropic_judge_fn
    raise ValueError(f"unknown LLM provider {provider!r}; choose from openai / anthropic / fake")


async def run_full_review_async(
    task_id: str,
    pr_ref: str,
    repo_root: Path | None,
    llm_provider: str = "fake",
    timeout: float = 60.0,
) -> None:
    """
    @brief W36f: 异步跑 full review (LLM + 对抗式), 进度推到 task store

    @param task_id       任务 ID (create_task 返)
    @param pr_ref        git diff range
    @param repo_root     git 仓库根 (None = cwd)
    @param llm_provider  openai / anthropic / fake
    @param timeout       LLM 调用超时 (秒)
    @note  本函数由 FastAPI BackgroundTasks 调度, 完成后 task 状态 = done
    @note  失败: task 状态 = error + error 字段
    """
    task = _TASK_STORE.get(task_id)
    if task is None:
        return  # task 已被清理, 静默丢
    _update_task(
        task_id, status="running", progress=5, log=[f"start full review, llm={llm_provider}"]
    )
    try:
        # 前置检查: cwd 必须是 git repo
        cwd: str | None = None
        if repo_root is not None:
            if not repo_root.exists():
                raise RuntimeError(f"repo_root {repo_root!r} does not exist")
            cwd = str(repo_root)
        check_path = Path(cwd) if cwd else Path.cwd()
        if not _is_git_repo(check_path):
            raise RuntimeError(f"not a git repository: {check_path}")
        _update_task(task_id, progress=15, log=["git repo OK"])
        # 临时设 env (agent_review 内部 REPO 读此 env)
        old_env: str | None = os.environ.get("AGENT_REVIEW_REPO")
        if cwd is not None:
            os.environ["AGENT_REVIEW_REPO"] = cwd
        sys.modules.pop("agent_review", None)
        try:
            # 延迟 import + 拿 LLM judge
            # (run_full_review 在 _run_full_in_thread 内导入, 避免此处未使用)
            judge_fn = llm_judge_factory(llm_provider)
            _update_task(task_id, progress=30, log=[f"judge factory OK ({llm_provider})"])
            # 跑 full review (在 thread 中执行, 不阻塞 event loop)
            # W37 真实流程: 传入 llm_provider 让 run_full_review 选 judge
            report = await asyncio.wait_for(
                asyncio.to_thread(_run_full_in_thread, pr_ref, judge_fn, llm_provider),
                timeout=timeout,
            )
            _update_task(task_id, progress=90, log=["full review done, serializing"])
            result = asdict(report)
            _update_task(task_id, status="done", progress=100, log=["done"], result=result)
        finally:
            if old_env is None:
                os.environ.pop("AGENT_REVIEW_REPO", None)
            else:
                os.environ["AGENT_REVIEW_REPO"] = old_env
            sys.modules.pop("agent_review", None)
    except TimeoutError:
        _update_task(task_id, status="error", error=f"timeout after {timeout}s")
    except RuntimeError as exc:
        _update_task(task_id, status="error", error=str(exc))
    except Exception as exc:  # noqa: BLE001
        log.exception("async full review failed")
        _update_task(task_id, status="error", error=f"{type(exc).__name__}: {exc}")


def _run_full_in_thread(
    pr_ref: str,
    judge_fn: Any,
    llm_provider: str = "fake",
) -> Any:
    """
    @brief 内部: 在 thread 中跑 full review (event loop 不阻塞)

    @param pr_ref       git diff range
    @param judge_fn     LLM judge 工厂返的 JudgeFn (W37 真实接入)
    @param llm_provider openai / anthropic / fake
    @return ReviewReport
    @note  W37 真实流程: 跑 run_full_review (AdversarialVerifier + judge_fn)
            - fake: deterministic
            - openai/anthropic: 真实 LLM 调用
    """
    import asyncio as _asyncio

    async def _wrapper() -> Any:
        from agent_review import run_full_review

        return await run_full_review(pr_ref, llm_provider=llm_provider)

    return _asyncio.run(_wrapper())


# 更新 __all__
__all__ = [
    "run_review_sync",
    "_is_git_repo",
    "ReviewTask",
    "create_task",
    "get_task",
    "subscribe_task",
    "cleanup_expired_tasks",
    "llm_judge_factory",
    "run_full_review_async",
]
