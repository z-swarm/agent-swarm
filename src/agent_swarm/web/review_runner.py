"""
@module agent_swarm.web.review_runner
@brief  P5-W36b — Web 与 tools/agent_review 之间的薄包装

职责:
  - 把 run_simple_review 的 cwd 切到指定 repo_root
  - 把 ReviewReport dataclass 序列化为 dict
  - 把异常 (非 git repo / 无 diff) 转成 RuntimeError 让 routes.py 处理

为什么不直接 import tools.agent_review?
  - tools/ 不在 PYTHONPATH 标准包路径下, import 不优雅
  - review_runner 是 web 模块的内部接口, 限定 import 边界
  - 未来 W36f 全模式 (LLM + 对抗式) 走异步时, 在此扩展

@note W36b 阶段只接 run_simple_review (W13 决策); 全模式留 W36f
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

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
            cwd=str(path), capture_output=True, text=True, timeout=5,
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
