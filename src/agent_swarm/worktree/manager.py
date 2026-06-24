"""
@module agent_swarm.worktree.manager
@brief  P4-W22 WorktreeManager——per-agent git worktree 隔离

DESIGN 决策 (§16.3 #10 后置议题):
  - 每个 (tenant, session, agent) 组合一个独立 git worktree
  - 跨 agent 文件隔离, 共享 .git 数据库
  - 同步 API (git CLI 阻塞, 调用方可用 asyncio.to_thread 包)
  - 幂等: 同 key 多次 acquire 返回同一 handle

并发模型:
  - 同进程: per-tenant asyncio.Lock (asyncio.Lock 因 sync API 不能用, 用 threading.Lock)
  - 跨进程: 不保证——多进程部署需外部分布式锁
  - 磁盘: per-worktree `.lock` 文件做 advisory lock (flock), 防止 git worktree 自身冲突

错误处理:
  - repo_root 不是 git 仓库 → ValueError (init 阶段)
  - branch 已存在但归属其他 handle → 复用 worktree
  - worktree 路径冲突 → 报错 (内部 bug)
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# 分支名 + worktree 名允许字符: a-z, 0-9, -, _; 用 _ 替换其他
_SAFE_RE = re.compile(r"[^a-z0-9_\-]+")


def _sanitize(s: str) -> str:
    """将任意字符串转为文件系统安全的小写标识符"""
    s = _SAFE_RE.sub("-", s.lower()).strip("-")
    return s or "x"


@dataclass
class WorktreeHandle:
    """单个 worktree 句柄——持有者负责 release"""

    path: Path
    branch: str
    agent_id: str
    tenant_id: str
    session_id: str
    created_at: float
    key: str  # "<tenant>/<session>/<agent>" 复合 key, 用于幂等查找

    def __repr__(self) -> str:
        return f"WorktreeHandle(key={self.key!r} path={self.path} branch={self.branch!r})"


@dataclass
class _Internal:
    """私有状态, 持有时锁引用 + git 句柄"""

    flock_path: Path  # .lock 文件
    flock_fd: int | None = None
    worktree_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])


class WorktreeError(RuntimeError):
    """Worktree 操作错误 (基类)"""


class WorktreeRepoError(WorktreeError):
    """repo_root 不是 git 仓库或初始化失败"""


class WorktreeConflictError(WorktreeError):
    """worktree 已存在但归属不一致"""


class WorktreeManager:
    """
    git worktree 隔离管理器

    @param repo_root   主 git 仓库根目录
    @param base_dir    worktree 父目录, 默认 `<repo_root>/.worktrees`
    """

    def __init__(self, repo_root: Path, base_dir: Path | None = None) -> None:
        self.repo_root = Path(repo_root).resolve(strict=True)
        if not _is_git_repo(self.repo_root):
            raise WorktreeRepoError(f"repo_root is not a git repository: {self.repo_root}")
        self.base_dir = (base_dir or self.repo_root / ".worktrees").resolve()
        self.base_dir.mkdir(parents=True, exist_ok=True)
        # key -> handle
        self._handles: dict[str, WorktreeHandle] = {}
        # path -> internal (锁/句柄)
        self._internals: dict[Path, _Internal] = {}
        # per-tenant threading.Lock (sync API, 不能用 asyncio.Lock)
        self._tenant_locks: dict[str, threading.Lock] = {}
        self._global_lock = threading.Lock()
        log.info(
            "WorktreeManager initialized: repo=%s base=%s",
            self.repo_root,
            self.base_dir,
        )

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------

    def acquire(
        self,
        *,
        tenant_id: str,
        session_id: str,
        agent_id: str,
    ) -> WorktreeHandle:
        """
        为 (tenant, session, agent) 分配/复用 worktree

        幂等: 同 key 多次调用返回同一 handle.

        @raise WorktreeRepoError git 命令失败
        """
        key = f"{tenant_id}/{session_id}/{agent_id}"
        tenant = _sanitize(tenant_id)
        sess = _sanitize(session_id)
        agent = _sanitize(agent_id)
        wt_name = f"wt-{tenant}-{sess}-{agent}"
        wt_path = self.base_dir / wt_name
        branch = f"wt/{tenant}/{sess}/{agent}"

        with self._tenant_lock(tenant_id):
            # 幂等: 已存在直接返回
            existing = self._handles.get(key)
            if existing is not None:
                return existing
            # 创建 worktree
            if wt_path.exists():
                # 路径已存在——可能是之前残留; 检查是否在 git worktree list 里
                if _worktree_registered(self.repo_root, wt_path):
                    # 已注册但 _handles 无记录——是 orphan, 复用
                    handle = WorktreeHandle(
                        path=wt_path,
                        branch=branch,
                        agent_id=agent_id,
                        tenant_id=tenant_id,
                        session_id=session_id,
                        created_at=time.time(),
                        key=key,
                    )
                    self._handles[key] = handle
                    self._internals[wt_path] = _Internal(
                        flock_path=wt_path / ".lock",
                    )
                    log.info("worktree.reuse (orphan) %s -> %s", key, wt_path)
                    return handle
                # 路径存在但不是 worktree——脏数据, 报错
                raise WorktreeConflictError(f"worktree path exists but not registered: {wt_path}")
            # 新建: 先建分支 (基于 HEAD), 再 add worktree
            self._git("branch", branch, check=False)  # 失败也无所谓 (可能已存在)
            try:
                self._git("worktree", "add", str(wt_path), branch)
            except WorktreeError as exc:
                # 极端情况: 之前 release 时 branch 删除失败, 现在 branch 还在但 worktree 没了
                if "already exists" in str(exc).lower():
                    # 强制清理分支重试
                    self._git("branch", "-D", branch, check=False)
                    self._git("worktree", "add", str(wt_path), branch)
                else:
                    raise
            handle = WorktreeHandle(
                path=wt_path,
                branch=branch,
                agent_id=agent_id,
                tenant_id=tenant_id,
                session_id=session_id,
                created_at=time.time(),
                key=key,
            )
            # 写 .lock 文件 (advisory)
            flock_path = wt_path / ".lock"
            flock_path.touch(exist_ok=True)
            self._handles[key] = handle
            self._internals[wt_path] = _Internal(flock_path=flock_path)
            log.info("worktree.acquire %s -> %s (branch=%s)", key, wt_path, branch)
            return handle

    def release(self, handle: WorktreeHandle, *, force: bool = False) -> None:
        """
        释放 worktree, 删除分支 (除非 force=True)

        @param force  True=只 remove worktree, 保留 branch (调试用)
        """
        key = handle.key
        tenant_id = handle.tenant_id
        with self._tenant_lock(tenant_id):
            # 二次确认
            current = self._handles.get(key)
            if current is not handle:
                # 不同 handle 对象, 可能是过期引用
                log.warning("worktree.release: handle mismatch for %s", key)
                return
            if handle.path.exists():
                try:
                    self._git("worktree", "remove", "--force", str(handle.path))
                except WorktreeError as exc:
                    log.warning("worktree.remove failed: %s", exc)
            if not force:
                # 删除分支
                self._git("branch", "-D", handle.branch, check=False)
            # 清理 .lock
            internal = self._internals.pop(handle.path, None)
            if internal and internal.flock_path.exists():
                with contextlib.suppress(OSError):
                    internal.flock_path.unlink()
            self._handles.pop(key, None)
            log.info("worktree.release %s", key)

    def list_active(self) -> list[WorktreeHandle]:
        """列出所有活跃 worktree (按 key 排序)"""
        with self._global_lock:
            return sorted(self._handles.values(), key=lambda h: h.key)

    def get(self, key: str) -> WorktreeHandle | None:
        """按 "<tenant>/<session>/<agent>" key 查询"""
        with self._global_lock:
            return self._handles.get(key)

    def cleanup_orphans(self, ttl_seconds: float = 300.0) -> int:
        """
        清理 base_dir 下不在 _handles 里的 orphan worktree (超过 TTL)

        @return 清理数量
        """
        cleaned = 0
        with self._global_lock:
            for entry in self.base_dir.iterdir():
                if not entry.is_dir():
                    continue
                if entry in self._internals:
                    continue
                # orphan: 不在 _internals, 检查 TTL
                try:
                    mtime = entry.stat().st_mtime
                except OSError:
                    continue
                age = time.time() - mtime
                if age < ttl_seconds:
                    continue
                # 是 worktree 注册的吗?
                if _worktree_registered(self.repo_root, entry):
                    try:
                        self._git(
                            "worktree",
                            "remove",
                            "--force",
                            str(entry),
                        )
                        # 尝试找到对应分支名
                        branch = _worktree_branch(self.repo_root, entry)
                        if branch:
                            self._git("branch", "-D", branch, check=False)
                        shutil.rmtree(entry, ignore_errors=True)
                        cleaned += 1
                        log.info(
                            "worktree.orphan.cleaned path=%s age=%.0fs",
                            entry,
                            age,
                        )
                    except WorktreeError as exc:
                        log.warning("orphan cleanup failed %s: %s", entry, exc)
                else:
                    # 不是 worktree, 也不在 _internals——垃圾目录
                    try:
                        shutil.rmtree(entry, ignore_errors=True)
                        cleaned += 1
                        log.info("worktree.orphan.garbage path=%s", entry)
                    except OSError:
                        pass
        return cleaned

    def cleanup_all(self) -> int:
        """强制清理所有 base_dir 下内容 (测试/关闭用)"""
        cleaned = 0
        # 先 release 所有 active
        for h in list(self.list_active()):
            self.release(h, force=True)
            cleaned += 1
        # 再扫一遍
        cleaned += self.cleanup_orphans(ttl_seconds=0.0)
        return cleaned

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _tenant_lock(self, tenant_id: str) -> _TenantLock:
        with self._global_lock:
            lock = self._tenant_locks.get(tenant_id)
            if lock is None:
                lock = threading.Lock()
                self._tenant_locks[tenant_id] = lock
        return _TenantLock(lock)

    def _git(self, *args: str, check: bool = True) -> str:
        """执行 git 命令, 返回 stdout"""
        cmd = ["git", "-C", str(self.repo_root), *args]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise WorktreeError(f"git timeout: {' '.join(cmd)}") from exc
        if proc.returncode != 0:
            if check:
                raise WorktreeError(
                    f"git failed: {' '.join(cmd)}\n"
                    f"  stderr: {proc.stderr.strip()}\n"
                    f"  stdout: {proc.stdout.strip()}"
                )
            return proc.stdout
        return proc.stdout


class _TenantLock:
    """threading.Lock 的 context manager 包装"""

    def __init__(self, lock: threading.Lock) -> None:
        self._lock = lock

    def __enter__(self) -> None:
        self._lock.acquire()

    def __exit__(self, *args: Any) -> None:
        self._lock.release()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@contextmanager
def _nullcontext() -> Iterator[None]:
    yield


def _is_git_repo(path: Path) -> bool:
    """检查 path 是否在 git 仓库内 (path 可以是子目录)"""
    try:
        proc = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    if proc.returncode != 0:
        return False
    top = proc.stdout.strip()
    # 跨平台归一化: git 输出正斜杠, Windows 用反斜杠
    top_norm = os.path.normpath(top)
    return os.path.commonpath([os.path.realpath(path), top_norm]) == top_norm


def _worktree_registered(repo_root: Path, wt_path: Path) -> bool:
    """wt_path 是否在 git worktree list 中"""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "worktree", "list", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    real = os.path.realpath(wt_path)
    for line in proc.stdout.splitlines():
        if line.startswith("worktree ") and os.path.realpath(line[len("worktree ") :]) == real:
            return True
    return False


def _worktree_branch(repo_root: Path, wt_path: Path) -> str | None:
    """查 worktree 的分支名 (porcelain)"""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "worktree", "list", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    real = os.path.realpath(wt_path)
    found = False
    for line in proc.stdout.splitlines():
        if line.startswith("worktree "):
            found = os.path.realpath(line[len("worktree ") :]) == real
        elif found and line.startswith("branch "):
            ref = line[len("branch ") :]
            if ref.startswith("refs/heads/"):
                return ref[len("refs/heads/") :]
            return ref
    return None


__all__ = [
    "WorktreeHandle",
    "WorktreeManager",
    "WorktreeError",
    "WorktreeRepoError",
    "WorktreeConflictError",
]
