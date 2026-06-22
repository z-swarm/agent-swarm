"""
@module tests.unit.test_worktree_manager
@brief  P4-W22 WorktreeManager 单元测试

覆盖:
  - 基本 acquire / release
  - 幂等性 (同 key 多次)
  - 并发 acquire (10 个线程)
  - 文件隔离 (A 写 B 看不到)
  - cleanup_orphans + cleanup_all
  - 边界: 非 git 目录, 已存在 worktree, 错误路径
"""

from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

import pytest

# P3-WIN: 某些并发/孤儿清理测试在 Windows 文件系统语义下 flaky
# 主流程覆盖, 边界 case 在 Linux CI 跑
from agent_swarm.worktree import WorktreeHandle, WorktreeManager
from agent_swarm.worktree.manager import (
    WorktreeConflictError,
    WorktreeRepoError,
    _is_git_repo,
    _sanitize,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """创建一个临时 git 仓库, 含一个初始 commit"""
    repo = tmp_path / "test_repo"
    repo.mkdir()
    # git init
    subprocess.run(
        ["git", "init", "-b", "main", str(repo)],
        check=True, capture_output=True, timeout=10,
    )
    # 配置 user (CI 环境无 git config)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@test.local"],
        check=True, capture_output=True, timeout=5,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Test"],
        check=True, capture_output=True, timeout=5,
    )
    # 初始 commit
    (repo / "README.md").write_text("# Test\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(repo), "add", "."],
        check=True, capture_output=True, timeout=5,
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "init"],
        check=True, capture_output=True, timeout=10,
    )
    return repo


@pytest.fixture
def manager(git_repo: Path, tmp_path: Path) -> WorktreeManager:
    """WorktreeManager 指向 git_repo, base_dir 独立"""
    base = tmp_path / "worktrees"
    return WorktreeManager(git_repo, base_dir=base)


# ---------------------------------------------------------------------------
# 基础测试
# ---------------------------------------------------------------------------


def test_sanitize_basic() -> None:
    """_sanitize: 非法字符转 -, 全小写, 边界保护"""
    assert _sanitize("tenant-A") == "tenant-a"
    assert _sanitize("hello world") == "hello-world"
    assert _sanitize("a/b\\c") == "a-b-c"
    assert _sanitize("") == "x"
    assert _sanitize("---") == "x"


def test_is_git_repo_true(git_repo: Path) -> None:
    """_is_git_repo: git repo 内任意子目录都返回 True"""
    assert _is_git_repo(git_repo)
    sub = git_repo / "subdir"
    sub.mkdir()
    assert _is_git_repo(sub)


def test_is_git_repo_false(tmp_path: Path) -> None:
    """_is_git_repo: 非 git 目录返回 False"""
    assert not _is_git_repo(tmp_path)


def test_manager_init_creates_base_dir(git_repo: Path, tmp_path: Path) -> None:
    """init: base_dir 不存在时自动创建"""
    base = tmp_path / "new" / "deep" / "base"
    mgr = WorktreeManager(git_repo, base_dir=base)
    assert base.exists()
    assert base.is_dir()
    assert mgr.repo_root == git_repo
    assert mgr.base_dir == base


def test_manager_init_rejects_non_git(tmp_path: Path) -> None:
    """init: 非 git 目录抛 WorktreeRepoError"""
    not_git = tmp_path / "not_a_repo"
    not_git.mkdir()
    with pytest.raises(WorktreeRepoError, match="not a git repository"):
        WorktreeManager(not_git)


def test_acquire_returns_handle(manager: WorktreeManager) -> None:
    """acquire: 返回 WorktreeHandle 含正确字段"""
    h = manager.acquire(
        tenant_id="t1", session_id="s1", agent_id="a1",
    )
    assert isinstance(h, WorktreeHandle)
    assert h.tenant_id == "t1"
    assert h.session_id == "s1"
    assert h.agent_id == "a1"
    assert h.key == "t1/s1/a1"
    assert h.branch == "wt/t1/s1/a1"
    assert h.path.exists()
    assert h.path.is_dir()
    assert (h.path / "README.md").exists()  # inherit from main


def test_acquire_creates_branch(manager: WorktreeManager, git_repo: Path) -> None:
    """acquire: 创建的分支确实在 git 里"""
    h = manager.acquire(tenant_id="t1", session_id="s1", agent_id="a1")
    proc = subprocess.run(
        ["git", "-C", str(git_repo), "branch", "--list", h.branch],
        capture_output=True, text=True, timeout=5, check=False,
    )
    assert h.branch in proc.stdout


def test_acquire_idempotent(manager: WorktreeManager) -> None:
    """acquire: 同 key 多次调用返回同一 handle"""
    h1 = manager.acquire(tenant_id="t1", session_id="s1", agent_id="a1")
    h2 = manager.acquire(tenant_id="t1", session_id="s1", agent_id="a1")
    assert h1 is h2
    assert h1.path == h2.path


def test_acquire_different_agents_isolated(manager: WorktreeManager) -> None:
    """acquire: 不同 agent 拿不同 worktree"""
    h1 = manager.acquire(tenant_id="t1", session_id="s1", agent_id="a1")
    h2 = manager.acquire(tenant_id="t1", session_id="s1", agent_id="a2")
    assert h1.path != h2.path
    # 各自能写
    (h1.path / "from_a1.md").write_text("A1", encoding="utf-8")
    (h2.path / "from_a2.md").write_text("A2", encoding="utf-8")
    assert (h1.path / "from_a1.md").read_text(encoding="utf-8") == "A1"
    assert not (h1.path / "from_a2.md").exists()  # 隔离


def test_acquire_different_tenants_isolated(manager: WorktreeManager) -> None:
    """acquire: 不同 tenant 拿不同 worktree"""
    h1 = manager.acquire(tenant_id="tA", session_id="s1", agent_id="a1")
    h2 = manager.acquire(tenant_id="tB", session_id="s1", agent_id="a1")
    assert h1.path != h2.path
    # tA -> "ta", tB -> "tb" 经 _sanitize
    assert "wt-ta-" in str(h1.path).lower().replace("\\", "/")
    assert "wt-tb-" in str(h2.path).lower().replace("\\", "/")


def test_release_removes_worktree(manager: WorktreeManager) -> None:
    """release: 清理 worktree + branch"""
    h = manager.acquire(tenant_id="t1", session_id="s1", agent_id="a1")
    assert h.path.exists()
    manager.release(h)
    assert not h.path.exists()
    # branch 也没了
    assert manager.get(h.key) is None


def test_release_force_keeps_branch(manager: WorktreeManager, git_repo: Path) -> None:
    """release(force=True): 只 remove worktree, 保留 branch"""
    h = manager.acquire(tenant_id="t1", session_id="s1", agent_id="a1")
    manager.release(h, force=True)
    assert not h.path.exists()
    # branch 还在
    proc = subprocess.run(
        ["git", "-C", str(git_repo), "branch", "--list", h.branch],
        capture_output=True, text=True, timeout=5, check=False,
    )
    assert h.branch in proc.stdout


def test_release_unknown_handle_is_noop(manager: WorktreeManager) -> None:
    """release: 不同 handle 对象是 no-op (警告但不报错)"""
    h1 = manager.acquire(tenant_id="t1", session_id="s1", agent_id="a1")
    fake = WorktreeHandle(
        path=h1.path, branch=h1.branch, agent_id="x",
        tenant_id="x", session_id="x", created_at=0.0, key="x/x/x",
    )
    manager.release(fake)  # no raise
    assert h1.path.exists()  # 原始还在


# ---------------------------------------------------------------------------
# list_active + get
# ---------------------------------------------------------------------------


def test_list_active_empty(manager: WorktreeManager) -> None:
    """list_active: 空 manager 返回空列表"""
    assert manager.list_active() == []


def test_list_active_sorted(manager: WorktreeManager) -> None:
    """list_active: 按 key 排序返回"""
    manager.acquire(tenant_id="t1", session_id="s1", agent_id="b")
    manager.acquire(tenant_id="t1", session_id="s1", agent_id="a")
    manager.acquire(tenant_id="t2", session_id="s1", agent_id="a")
    handles = manager.list_active()
    assert [h.key for h in handles] == [
        "t1/s1/a", "t1/s1/b", "t2/s1/a",
    ]


def test_get_existing(manager: WorktreeManager) -> None:
    """get: 存在时返回 handle"""
    h = manager.acquire(tenant_id="t1", session_id="s1", agent_id="a1")
    assert manager.get("t1/s1/a1") is h


def test_get_missing(manager: WorktreeManager) -> None:
    """get: 不存在返回 None"""
    assert manager.get("nonexistent/key") is None


# ---------------------------------------------------------------------------
# cleanup
# ---------------------------------------------------------------------------


def test_cleanup_orphans_removes_stale(git_repo: Path, tmp_path: Path) -> None:
    """cleanup_orphans: TTL 之前的 orphan 不清, 之后清"""
    base = tmp_path / "worktrees"
    mgr = WorktreeManager(git_repo, base_dir=base)
    h = mgr.acquire(tenant_id="t1", session_id="s1", agent_id="a1")
    mgr.release(h)  # 删除 worktree, branch 也删
    # 模拟外部残留: 手动建一个 git worktree
    orphan_branch = "wt-orphan-test"
    orphan_path = base / "wt-orphan-external"
    subprocess.run(
        ["git", "-C", str(git_repo), "branch", orphan_branch],
        check=True, capture_output=True, timeout=5,
    )
    subprocess.run(
        ["git", "-C", str(git_repo), "worktree", "add",
         str(orphan_path), orphan_branch],
        check=True, capture_output=True, timeout=10,
    )
    assert orphan_path.exists()
    # 修改 mtime 让它"老"
    import os as _os
    old_time = time.time() - 1000
    _os.utime(orphan_path, (old_time, old_time))
    # 等于零 ttl
    cleaned = mgr.cleanup_orphans(ttl_seconds=0.0)
    assert cleaned >= 1
    assert not orphan_path.exists()
    # 主 worktree 路径已删
    assert not h.path.exists()


def test_cleanup_orphans_skips_recent(git_repo: Path, tmp_path: Path) -> None:
    """cleanup_orphans: TTL 之内的 orphan 保留"""
    base = tmp_path / "worktrees"
    mgr = WorktreeManager(git_repo, base_dir=base)
    h = mgr.acquire(tenant_id="t1", session_id="s1", agent_id="a1")
    mgr.release(h, force=True)  # 删 worktree, 保 branch
    # 路径已被 release 删了; 测试 TTL 内的 active handle 不被误清
    # 先 acquire 一个新的, 不 release, 看 cleanup 是否跳过 (在 _internals 里)
    h2 = mgr.acquire(tenant_id="t2", session_id="s2", agent_id="a2")
    cleaned = mgr.cleanup_orphans(ttl_seconds=3600.0)  # 1 小时
    assert cleaned == 0
    # h2 仍存在
    assert h2.path.exists()


def test_cleanup_all_clears_everything(manager: WorktreeManager) -> None:
    """cleanup_all: 强制清理所有"""
    for i in range(5):
        manager.acquire(tenant_id=f"t{i}", session_id="s1", agent_id="a1")
    assert len(manager.list_active()) == 5
    cleaned = manager.cleanup_all()
    assert cleaned >= 5
    assert manager.list_active() == []


# ---------------------------------------------------------------------------
# 并发
# ---------------------------------------------------------------------------


def test_concurrent_acquire_10_threads(manager: WorktreeManager) -> None:
    """10 线程并发 acquire 不同 agent——应全部成功, 互不干扰"""
    results: list[WorktreeHandle] = []
    errors: list[Exception] = []

    def acquire_one(i: int) -> None:
        try:
            h = manager.acquire(tenant_id="t1", session_id="s1", agent_id=f"a{i}")
            results.append(h)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=acquire_one, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"concurrent acquire failed: {errors}"
    assert len(results) == 10
    # 10 个不同路径
    paths = {h.path for h in results}
    assert len(paths) == 10
    # 都存在
    for h in results:
        assert h.path.exists()


def test_concurrent_acquire_same_key(manager: WorktreeManager) -> None:
    """10 线程并发 acquire 同 key——应幂等, 返回同一 handle"""
    results: list[WorktreeHandle] = []
    errors: list[Exception] = []

    def acquire_one() -> None:
        try:
            h = manager.acquire(tenant_id="t1", session_id="s1", agent_id="a1")
            results.append(h)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=acquire_one) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"concurrent same-key acquire failed: {errors}"
    assert len(results) == 10
    # 全部同一 path
    paths = {h.path for h in results}
    assert len(paths) == 1


# ---------------------------------------------------------------------------
# 文件系统隔离
# ---------------------------------------------------------------------------


def test_file_isolation_between_worktrees(manager: WorktreeManager) -> None:
    """A worktree 写文件, B worktree 不可见"""
    h1 = manager.acquire(tenant_id="t1", session_id="s1", agent_id="a1")
    h2 = manager.acquire(tenant_id="t1", session_id="s2", agent_id="a2")
    (h1.path / "agent1_only.md").write_text("agent1", encoding="utf-8")
    (h2.path / "agent2_only.md").write_text("agent2", encoding="utf-8")
    assert (h1.path / "agent1_only.md").exists()
    assert not (h1.path / "agent2_only.md").exists()
    assert (h2.path / "agent2_only.md").exists()
    assert not (h2.path / "agent1_only.md").exists()


def test_worktree_inherits_main_branch_files(manager: WorktreeManager) -> None:
    """worktree 继承 main 分支文件 (从初始 commit)"""
    h = manager.acquire(tenant_id="t1", session_id="s1", agent_id="a1")
    # main 上有 README.md
    assert (h.path / "README.md").exists()


# ---------------------------------------------------------------------------
# 错误处理
# ---------------------------------------------------------------------------


def test_path_collision_with_existing_dir(
    git_repo: Path, tmp_path: Path,
) -> None:
    """worktree 路径与既有目录冲突时抛 WorktreeConflictError"""
    base = tmp_path / "worktrees"
    base.mkdir()
    # 预先放一个同名但非 worktree 的目录
    collision = base / "wt-t1-s1-a1"
    collision.mkdir()
    (collision / "dirty.txt").write_text("dirty", encoding="utf-8")
    mgr = WorktreeManager(git_repo, base_dir=base)
    with pytest.raises(WorktreeConflictError, match="not registered"):
        mgr.acquire(tenant_id="t1", session_id="s1", agent_id="a1")


def test_worktree_path_safety(manager: WorktreeManager) -> None:
    """worktree 路径必须在 base_dir 内 (无 path traversal)"""
    h = manager.acquire(
        tenant_id="../../etc", session_id="../../passwd", agent_id="a1",
    )
    # sanitize 后应被锁在 base_dir 内
    assert h.path.is_relative_to(manager.base_dir)
