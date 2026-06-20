"""
@module tests.unit.test_session_binding
@brief  W17-②④ SessionBindingManager 单元测试——DESIGN §8.5

覆盖:
  - ChannelIdentity 注册 + 查询
  - 跨通道身份解析 (resolve_user)
  - Session 绑定 / 查询
  - bind_or_get_session: 跨通道共享 session (W17 DoD ③)
  - SQLite 持久化路径
"""

from __future__ import annotations

import tempfile
import threading
from pathlib import Path

import pytest

from agent_swarm.core.session_binding import (
    ChannelIdentity,
    SessionBinding,
    SessionBindingManager,
)


# ---------------------------------------------------------------------------
# ChannelIdentity
# ---------------------------------------------------------------------------


def test_register_and_get_identity() -> None:
    mgr = SessionBindingManager()
    mgr.register_identity(
        tenant_id="acme", identity_key="ou_abc",
        channel="lark", user_id="alice@example.com",
    )
    identity = mgr.get_identity("acme", "ou_abc")
    assert identity is not None
    assert identity.user_id == "alice@example.com"
    assert identity.channel == "lark"


def test_get_identity_unregistered() -> None:
    mgr = SessionBindingManager()
    assert mgr.get_identity("acme", "ghost") is None


def test_resolve_user() -> None:
    mgr = SessionBindingManager()
    mgr.register_identity(
        tenant_id="acme", identity_key="ou_abc",
        channel="lark", user_id="alice@example.com",
    )
    user = mgr.resolve_user("acme", "ou_abc")
    assert user == "alice@example.com"


def test_resolve_user_unregistered_returns_none() -> None:
    mgr = SessionBindingManager()
    assert mgr.resolve_user("acme", "ghost") is None


def test_register_identity_without_user_id() -> None:
    mgr = SessionBindingManager()
    mgr.register_identity(
        tenant_id="acme", identity_key="cli-session-1",
        channel="cli",
    )
    identity = mgr.get_identity("acme", "cli-session-1")
    assert identity is not None
    assert identity.user_id is None
    # resolve_user 返 None（无 user_id）
    assert mgr.resolve_user("acme", "cli-session-1") is None


# ---------------------------------------------------------------------------
# Session 绑定
# ---------------------------------------------------------------------------


def test_bind_and_get_session() -> None:
    mgr = SessionBindingManager()
    mgr.bind_session("acme", "alice-cli", "sess-1", "cli")
    assert mgr.get_session("acme", "alice-cli") == "sess-1"


def test_bind_session_replaces() -> None:
    """重复绑定覆盖"""
    mgr = SessionBindingManager()
    mgr.bind_session("acme", "alice", "sess-1", "cli")
    mgr.bind_session("acme", "alice", "sess-2", "lark")
    assert mgr.get_session("acme", "alice") == "sess-2"


def test_get_session_unbound() -> None:
    mgr = SessionBindingManager()
    assert mgr.get_session("acme", "ghost") is None


def test_list_bindings() -> None:
    mgr = SessionBindingManager()
    mgr.bind_session("acme", "a", "s1", "cli")
    mgr.bind_session("acme", "b", "s2", "lark")
    mgr.bind_session("other", "c", "s3", "web")
    all_b = mgr.list_bindings()
    acme_b = mgr.list_bindings(tenant_id="acme")
    assert len(all_b) == 3
    assert len(acme_b) == 2


# ---------------------------------------------------------------------------
# bind_or_get_session (W17 DoD ③)
# ---------------------------------------------------------------------------


def test_bind_or_get_creates_new() -> None:
    mgr = SessionBindingManager()
    counter = [0]

    def factory():
        counter[0] += 1
        return f"sess-{counter[0]}"

    sid = mgr.bind_or_get_session("acme", "alice", "cli", factory)
    assert sid == "sess-1"
    assert counter[0] == 1


def test_bind_or_get_reuses_existing() -> None:
    mgr = SessionBindingManager()
    counter = [0]

    def factory():
        counter[0] += 1
        return f"sess-{counter[0]}"

    sid1 = mgr.bind_or_get_session("acme", "alice", "cli", factory)
    sid2 = mgr.bind_or_get_session("acme", "alice", "lark", factory)
    assert sid1 == sid2  # 同一身份复用
    assert counter[0] == 1


def test_bind_or_get_shares_session_via_user_id() -> None:
    """W17 DoD ③ 核心: 飞书 open_id 和 CLI user_id 共享同一 session"""
    mgr = SessionBindingManager()
    counter = [0]

    def factory():
        counter[0] += 1
        return f"sess-{counter[0]}"

    # 飞书 @bot: open_id 触发, 注册为 alice
    mgr.register_identity(
        tenant_id="acme", identity_key="ou_abc123",
        channel="lark", user_id="alice@example.com",
    )
    # 1) Lark 触发: 创建 session sess-1
    sid_lark = mgr.bind_or_get_session("acme", "ou_abc123", "lark", factory)
    assert sid_lark == "sess-1"
    assert counter[0] == 1

    # 2) CLI 触发: CLI 端用 user_id="alice@example.com" 直接调
    #    (CLI 端一般 user_id 已知, 不走 register_identity)
    sid_cli = mgr.bind_or_get_session("acme", "alice@example.com", "cli", factory)
    # 期望: 共享 sess-1（跨通道合并）
    assert sid_cli == "sess-1", f"expected shared session, got {sid_cli}"
    assert counter[0] == 1, "factory should not be called again"


def test_bind_or_get_tenant_isolation() -> None:
    """不同 tenant 的同名 user_id 不共享 session"""
    mgr = SessionBindingManager()
    counter = [0]

    def factory():
        counter[0] += 1
        return f"sess-{counter[0]}"

    mgr.register_identity(
        tenant_id="acme", identity_key="ou_x",
        channel="lark", user_id="alice@example.com",
    )
    sid1 = mgr.bind_or_get_session("acme", "ou_x", "lark", factory)
    # 另一 tenant 同样 user_id
    sid2 = mgr.bind_or_get_session("beta", "alice@example.com", "cli", factory)
    assert sid1 != sid2  # tenant_id 不同 -> 不同 session
    assert counter[0] == 2


# ---------------------------------------------------------------------------
# SQLite 持久化
# ---------------------------------------------------------------------------


def test_sqlite_persistence(tmp_path: Path) -> None:
    db = tmp_path / "bindings.db"
    mgr1 = SessionBindingManager(db_path=db)
    mgr1.register_identity(
        tenant_id="acme", identity_key="ou_abc",
        channel="lark", user_id="alice@example.com",
    )
    mgr1.bind_session("acme", "ou_abc", "sess-1", "lark")
    del mgr1

    # 新 manager 重新加载
    mgr2 = SessionBindingManager(db_path=db)
    assert mgr2.get_session("acme", "ou_abc") == "sess-1"
    assert mgr2.resolve_user("acme", "ou_abc") == "alice@example.com"


def test_concurrent_bind_session_thread_safe() -> None:
    """并发线程绑定同一 identity_key —— 应保证 thread-safe"""
    mgr = SessionBindingManager()
    results: list[str] = []
    errors: list[Exception] = []

    def worker(worker_id: int) -> None:
        try:
            sid = f"sess-{worker_id}"
            mgr.bind_session("acme", "shared-key", sid, "cli")
            results.append(mgr.get_session("acme", "shared-key") or "")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    # 至少一个 sess-N 胜出
    assert any(r.startswith("sess-") for r in results)
    # 最终值应是其中之一
    final = mgr.get_session("acme", "shared-key")
    assert final and final.startswith("sess-")


# ---------------------------------------------------------------------------
# Clear / 清空
# ---------------------------------------------------------------------------


def test_clear() -> None:
    mgr = SessionBindingManager()
    mgr.register_identity("t1", "k1", "lark", "u1")
    mgr.bind_session("t1", "k1", "s1", "lark")
    mgr.clear()
    assert mgr.get_session("t1", "k1") is None
    assert mgr.get_identity("t1", "k1") is None


def test_clear_with_sqlite(tmp_path: Path) -> None:
    db = tmp_path / "bindings.db"
    mgr = SessionBindingManager(db_path=db)
    mgr.register_identity("t1", "k1", "lark", "u1")
    mgr.bind_session("t1", "k1", "s1", "lark")
    mgr.clear()
    # SQLite 持久化下 clear 应该清掉
    assert mgr.get_session("t1", "k1") is None
