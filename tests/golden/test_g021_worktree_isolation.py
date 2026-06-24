"""
@module tests.golden.test_g021_worktree_isolation
@brief  P4-W23 G-021 Golden Case: 3 agent 共享 repo, 各自 worktree 写文件无冲突

DoD (P4 W23):
  - 3 agent 同时 acquire worktree, 互不阻塞
  - 各自 worktree 写 100 文件, 内容含 agent_id
  - worktree 之间的文件系统严格隔离
  - branch / commit 各自独立
  - cleanup 后 git worktree list 不含已释放项
"""

from __future__ import annotations

import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from agent_swarm.worktree import WorktreeManager


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "g021_repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main", str(repo)],
        check=True,
        capture_output=True,
        timeout=10,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "g021@t.local"],
        check=True,
        capture_output=True,
        timeout=5,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "G021"],
        check=True,
        capture_output=True,
        timeout=5,
    )
    (repo / "README.md").write_text("# G-021\n", encoding="utf-8")
    (repo / "shared.txt").write_text("shared", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(repo), "add", "."],
        check=True,
        capture_output=True,
        timeout=5,
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "init"],
        check=True,
        capture_output=True,
        timeout=10,
    )
    return repo


def test_g021_three_agents_isolated_worktrees(git_repo: Path, tmp_path: Path) -> None:
    """
    G-021 主流程:
      1. 3 agent (alpha/beta/gamma) 共享同一 tenant
      2. 各自 acquire worktree
      3. 并发各写 100 文件, 内容含 agent_id
      4. 验证隔离: alpha 看不到 beta/gamma 的文件
      5. 各自 commit + release
      6. main 分支不受影响
    """
    base = tmp_path / "g021_worktrees"
    mgr = WorktreeManager(git_repo, base_dir=base)

    # 1-2. Acquire 3 worktrees
    h_alpha = mgr.acquire(tenant_id="g021", session_id="s1", agent_id="alpha")
    h_beta = mgr.acquire(tenant_id="g021", session_id="s1", agent_id="beta")
    h_gamma = mgr.acquire(tenant_id="g021", session_id="s1", agent_id="gamma")
    handles = {"alpha": h_alpha, "beta": h_beta, "gamma": h_gamma}

    # 3. 并发各写 100 文件
    n_files = 100

    def write_files(agent_id: str, handle) -> None:
        for i in range(n_files):
            f = handle.path / f"{agent_id}_file_{i:03d}.txt"
            f.write_text(f"content from {agent_id} #{i}", encoding="utf-8")

    with ThreadPoolExecutor(max_workers=3) as ex:
        list(
            ex.map(
                lambda kv: write_files(kv[0], kv[1]),
                handles.items(),
            )
        )

    # 4. 验证隔离: alpha 只能看到自己的 100 文件
    for agent_id, handle in handles.items():
        own = sorted(handle.path.glob(f"{agent_id}_file_*.txt"))
        assert len(own) == n_files, f"{agent_id} expected {n_files} files, got {len(own)}"
        # 不应看到其他 agent 的文件
        for other in handles:
            if other == agent_id:
                continue
            other_files = list(handle.path.glob(f"{other}_file_*.txt"))
            assert other_files == [], (
                f"{agent_id} should not see {other}'s files, but found: {other_files[:3]}..."
            )

    # 5. 各自 commit
    for agent_id, handle in handles.items():
        subprocess.run(
            ["git", "-C", str(handle.path), "add", "."],
            check=True,
            capture_output=True,
            timeout=10,
        )
        subprocess.run(
            ["git", "-C", str(handle.path), "commit", "-m", f"{agent_id} writes"],
            check=True,
            capture_output=True,
            timeout=10,
        )

    # 验证各自 branch 有 commit
    for agent_id, handle in handles.items():
        proc = subprocess.run(
            ["git", "-C", str(git_repo), "log", handle.branch, "--oneline"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        assert f"{agent_id} writes" in proc.stdout, (
            f"branch {handle.branch} should have {agent_id}'s commit"
        )

    # 6. main 分支不受影响 (只有 init commit)
    proc = subprocess.run(
        ["git", "-C", str(git_repo), "log", "main", "--oneline"],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert "alpha writes" not in proc.stdout
    assert "beta writes" not in proc.stdout
    assert "gamma writes" not in proc.stdout
    assert "init" in proc.stdout

    # 7. Release all + verify cleanup
    mgr.cleanup_all()
    proc = subprocess.run(
        ["git", "-C", str(git_repo), "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    # 只剩 main worktree
    worktree_count = proc.stdout.count("\nworktree ") + (
        1 if proc.stdout.startswith("worktree ") else 0
    )
    assert worktree_count == 1, f"expected 1 worktree, got {worktree_count}"


def test_g021_concurrent_acquire_no_conflict(git_repo: Path, tmp_path: Path) -> None:
    """
    G-021 变体: 10 agent 并发 acquire——不冲突, 不丢分支
    """
    base = tmp_path / "g021_concurrent"
    mgr = WorktreeManager(git_repo, base_dir=base)

    n = 10
    with ThreadPoolExecutor(max_workers=n) as ex:
        handles = list(
            ex.map(
                lambda i: mgr.acquire(
                    tenant_id="g021",
                    session_id="s1",
                    agent_id=f"agent{i}",
                ),
                range(n),
            )
        )

    assert len(handles) == n
    assert len({h.path for h in handles}) == n  # 10 个不同路径
    # git worktree list 应该有 10 个 + main = 11 行 (--porcelain 模式)
    proc = subprocess.run(
        ["git", "-C", str(git_repo), "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    worktree_count = proc.stdout.count("\nworktree ") + (
        1 if proc.stdout.startswith("worktree ") else 0
    )
    assert worktree_count == n + 1, f"expected {n + 1} worktrees, got {worktree_count}"
    mgr.cleanup_all()


def test_g021_cross_tenant_isolation(git_repo: Path, tmp_path: Path) -> None:
    """
    G-021 跨租户: tenantA 的 worktree 对 tenantB 完全不可见
    """
    base = tmp_path / "g021_cross_tenant"
    mgr = WorktreeManager(git_repo, base_dir=base)

    h_A1 = mgr.acquire(tenant_id="tenantA", session_id="s1", agent_id="a1")
    h_B1 = mgr.acquire(tenant_id="tenantB", session_id="s1", agent_id="a1")
    # 同一 agent_id 但不同 tenant → 独立 worktree
    assert h_A1.path != h_B1.path
    assert h_A1.branch != h_B1.branch
    # tenantA -> "tenanta" (sanitize 去掉非 a-z0-9_-)
    assert "tenanta" in str(h_A1.path).lower()
    assert "tenantb" in str(h_B1.path).lower()
