"""
@module agent_swarm.core.context
@brief  Patched asyncio helpers + audit 工具——DESIGN §16.3 #11 + W17 DoD ⑧

DESIGN §16.3 #11 已知问题：
  "SecurityContext 在 asyncio.create_task 中丢失风险——是否需要全局拦截器？
   倾向：约定 + lint 规则；不引入魔法"

W17 落地分两阶段 (P3-PLAN-v2 W17 ⑧)：
  W17a (前 2 天): 扫现有 9 处 asyncio.create_task 用法 + 改 context= 显式传
    → tools/audit_create_task.py 工具扫现有代码
  W17b (后 3 天): lint 规则启用 (no-bare-asyncio-create-task) — 只对 src/agent_swarm/ 生效
    → tools/no_bare_create_task.py 守门脚本

@note 本文件仅 W17a 落地:
  - patched_create_task() wrapper — 显式取当前 SecurityContext, 注入 asyncio task context
  - audit_create_task.py 工具扫描 src/

Python 3.11+ asyncio.create_task 默认会 copy_context(), 但显式传 context= 更稳:
  - 与 Python 3.10 兼容 (3.10 不会自动复制)
  - 行为明确, 便于排查
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

from agent_swarm.security.context import (
    SecurityContextManager,
)

log = logging.getLogger(__name__)


def patched_create_task(
    coro: Coroutine[Any, Any, Any],
    *,
    name: str | None = None,
) -> asyncio.Task[Any]:
    """
    Patched asyncio.create_task() — 自动注入当前 SecurityContext 到新 task 的 contextvars

    @param coro  协程对象
    @param name  任务名 (用于日志/debug)
    @return asyncio.Task

    @note 用法:
        from agent_swarm.core.context import patched_create_task
        task = patched_create_task(some_coro(), name="my-task")
    @note 不传 context= 的 asyncio.create_task() 在 Python 3.10 不会自动复制
          contextvars; 3.11+ 会自动复制但显式传更稳
    @note 业务代码应**优先**用本 wrapper——CI lint (W17b) 禁止裸 asyncio.create_task
    """
    # 取当前 SecurityContext (在 scope/async_scope 内)
    try:
        ctx = SecurityContextManager.current_or_default()
    except Exception:  # noqa: BLE001
        ctx = None

    # asyncio.create_task 接受 context= kwarg (3.11+); 3.10 不支持
    # 显式传 context 让 3.10 也安全; 3.11+ 同结果
    task_kwargs: dict[str, Any] = {}
    if name is not None:
        task_kwargs["name"] = name
    if ctx is not None:
        # type: ignore[arg-type]  -- context= 是 3.11+ kwarg, 3.10 静默忽略
        task_kwargs["context"] = ctx.asyncio_context()
    return asyncio.create_task(coro, **task_kwargs)  # type: ignore[arg-type]


__all__ = ["patched_create_task"]
