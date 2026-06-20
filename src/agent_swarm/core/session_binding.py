"""
@module agent_swarm.core.session_binding
@brief  W17-② SessionBindingManager——跨通道 Session 合并

P3-PLAN-v2 W17 DoD ③ ④：
  - ③ SessionBindingManager 支持同身份跨通道绑定
    (用户在飞书 @bot + CLI 用同一 user_id → 共享 swarm session)
  - ④ 通道身份合并：飞书 open_id ↔ CLI user_id 通过
    tenant_id + identity_key 映射；映射表走 SQLite

@note W17 范围 (内存版):
  - 在内存 dict 中维护 (tenant_id, identity_key) -> session_id 映射
  - W18 可选: 接入 Redis/PostgreSQL 做多进程共享
  - W17-3 跨通道模块: 飞书/CLI/Web 三种 source 的统一解析

存储 schema (内存):
  {
    "<tenant_id>:<identity_key>": "<session_id>",
    ...
  }
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class ChannelIdentity:
    """单条通道身份记录——DESIGN §8.5 + W17 DoD ④"""

    tenant_id: str
    identity_key: str  # 通道身份（飞书 open_id / CLI user_id / Web session_id）
    channel: str        # "lark" / "cli" / "web"
    user_id: str | None = None  # 跨通道统一 ID（通常是 email）
    session_id: str | None = None
    created_at: datetime = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.created_at is None:
            self.created_at = datetime.now()


@dataclass
class SessionBinding:
    """单条 session 绑定记录——DESIGN §8.5 + W17 DoD ③"""

    tenant_id: str
    identity_key: str
    session_id: str
    channel: str
    bound_at: datetime = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.bound_at is None:
            self.bound_at = datetime.now()


class SessionBindingManager:
    """
    跨通道 Session 绑定管理器——DESIGN §8.5

    @note W17 范围 (内存版 + 可选 SQLite 持久化)
    @note 多进程场景: 需配 db_path 走 SQLite (W17-3 跨通道模式)
    """

    def __init__(self, db_path: Path | None = None) -> None:
        """
        @param db_path  None = 内存 (单进程);  给路径 = SQLite 持久化 (多进程)
        """
        self._lock = threading.RLock()
        self._bindings: dict[str, SessionBinding] = {}  # key: tenant_id|identity_key
        self._identities: dict[str, ChannelIdentity] = {}  # key: tenant_id|identity_key
        self._user_to_identities: dict[str, set[str]] = {}  # user_id -> {identity_key}
        self._db_path = db_path
        self._db_conn: sqlite3.Connection | None = None
        if db_path is not None:
            self._init_db(db_path)

    def _init_db(self, db_path: Path) -> None:
        """初始化 SQLite 持久化"""
        self._db_conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._db_conn.execute("""
            CREATE TABLE IF NOT EXISTS session_bindings (
                tenant_id   TEXT NOT NULL,
                identity_key TEXT NOT NULL,
                session_id  TEXT NOT NULL,
                channel     TEXT NOT NULL,
                bound_at    REAL NOT NULL,
                PRIMARY KEY (tenant_id, identity_key)
            )
        """)
        self._db_conn.execute("""
            CREATE TABLE IF NOT EXISTS channel_identities (
                tenant_id   TEXT NOT NULL,
                identity_key TEXT NOT NULL,
                channel     TEXT NOT NULL,
                user_id     TEXT,
                created_at  REAL NOT NULL,
                PRIMARY KEY (tenant_id, identity_key)
            )
        """)
        self._db_conn.commit()

    # ------------------------------------------------------------------
    # 通道身份管理
    # ------------------------------------------------------------------

    def register_identity(
        self,
        tenant_id: str,
        identity_key: str,
        channel: str,
        user_id: str | None = None,
    ) -> ChannelIdentity:
        """
        注册通道身份——飞书/CLI/Web 等任何 source 都走这个入口

        @return ChannelIdentity 对象 (含 user_id, channel, etc.)
        """
        with self._lock:
            identity = ChannelIdentity(
                tenant_id=tenant_id,
                identity_key=identity_key,
                channel=channel,
                user_id=user_id,
            )
            key = f"{tenant_id}|{identity_key}"
            self._identities[key] = identity
            if self._db_conn is not None:
                self._db_conn.execute(
                    """INSERT OR REPLACE INTO channel_identities
                       (tenant_id, identity_key, channel, user_id, created_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (tenant_id, identity_key, channel, user_id,
                     identity.created_at.timestamp()),
                )
                self._db_conn.commit()
            if user_id:
                self._user_to_identities.setdefault(user_id, set()).add(key)
            return identity

    def get_identity(
        self, tenant_id: str, identity_key: str,
    ) -> ChannelIdentity | None:
        """取已注册身份——None 表示未注册"""
        key = f"{tenant_id}|{identity_key}"
        if self._db_conn is not None:
            row = self._db_conn.execute(
                "SELECT channel, user_id, created_at FROM channel_identities "
                "WHERE tenant_id=? AND identity_key=?",
                (tenant_id, identity_key),
            ).fetchone()
            if row is None:
                return None
            return ChannelIdentity(
                tenant_id=tenant_id,
                identity_key=identity_key,
                channel=row[0],
                user_id=row[1],
                created_at=datetime.fromtimestamp(row[2]),
            )
        # 内存: 直接查 identities 表
        return self._identities.get(key)

    def resolve_user(
        self, tenant_id: str, identity_key: str,
    ) -> str | None:
        """
        跨通道身份解析——返回统一 user_id

        @note 用于 SessionBindingManager.bind_or_get_session:
              飞书 @bot 触发 (identity_key = open_id) → 查 user_id
              → 用 user_id 查/建 session
        """
        identity = self.get_identity(tenant_id, identity_key)
        return identity.user_id if identity else None

    # ------------------------------------------------------------------
    # Session 绑定
    # ------------------------------------------------------------------

    def bind_session(
        self,
        tenant_id: str,
        identity_key: str,
        session_id: str,
        channel: str,
    ) -> SessionBinding:
        """绑定 (tenant_id, identity_key) -> session_id"""
        with self._lock:
            binding = SessionBinding(
                tenant_id=tenant_id, identity_key=identity_key,
                session_id=session_id, channel=channel,
            )
            key = f"{tenant_id}|{identity_key}"
            self._bindings[key] = binding
            if self._db_conn is not None:
                self._db_conn.execute(
                    """INSERT OR REPLACE INTO session_bindings
                       (tenant_id, identity_key, session_id, channel, bound_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (tenant_id, identity_key, session_id, channel,
                     binding.bound_at.timestamp()),
                )
                self._db_conn.commit()
            return binding

    def get_session(
        self, tenant_id: str, identity_key: str,
    ) -> str | None:
        """取 (tenant_id, identity_key) 绑定的 session_id"""
        with self._lock:
            key = f"{tenant_id}|{identity_key}"
            binding = self._bindings.get(key)
            if binding is None and self._db_conn is not None:
                row = self._db_conn.execute(
                    "SELECT session_id FROM session_bindings "
                    "WHERE tenant_id=? AND identity_key=?",
                    (tenant_id, identity_key),
                ).fetchone()
                if row is not None:
                    return row[0]
            return binding.session_id if binding else None

    def bind_or_get_session(
        self,
        tenant_id: str,
        identity_key: str,
        channel: str,
        factory: Any,  # Callable[[], str] — create new session
    ) -> str:
        """
        跨通道 Session 共享——W17 DoD ③

        @param factory  创建新 session 的回调
        @return 已有 session_id (复用) 或新创建 (factory 调用)

        共享逻辑 (按优先级):
          1) (tenant_id, identity_key) 直接查
          2) tenant_id 内, identity_key 作为 user_id 的注册记录查到 user_id
             → 遍历该 user_id 下的所有 identity_key, 看哪个已绑
          3) 全新 session
        """
        existing = self.get_session(tenant_id, identity_key)
        if existing is not None:
            return existing
        # 跨通道合并: 通过 user_id 找同一用户已绑 session
        user_id = self.resolve_user(tenant_id, identity_key)
        if user_id is None:
            # identity_key 可能直接是 user_id (CLI 端常见)
            user_id = identity_key
        for ids_key in self._user_to_identities.get(user_id, set()):
            t, k = ids_key.split("|", 1)
            if t != tenant_id:
                continue
            bound = self.get_session(t, k)
            if bound and bound != self.get_session(tenant_id, identity_key):
                self.bind_session(tenant_id, identity_key, bound, channel)
                return bound
        # 全新 session
        new_id = factory()
        self.bind_session(tenant_id, identity_key, new_id, channel)
        return new_id

    def list_bindings(self, tenant_id: str | None = None) -> list[SessionBinding]:
        """列绑定记录"""
        with self._lock:
            if tenant_id is None:
                return list(self._bindings.values())
            return [
                b for b in self._bindings.values()
                if b.tenant_id == tenant_id
            ]

    def clear(self) -> None:
        """清空（测试用）"""
        with self._lock:
            self._bindings.clear()
            self._identities.clear()
            self._user_to_identities.clear()
            if self._db_conn is not None:
                self._db_conn.execute("DELETE FROM session_bindings")
                self._db_conn.execute("DELETE FROM channel_identities")
                self._db_conn.commit()


__all__ = [
    "ChannelIdentity",
    "SessionBinding",
    "SessionBindingManager",
]
