"""
@module agent_swarm.security.secret_manager
@brief  W20-①② 密钥管理——SecretManager ABC + EnvSecretManager + VaultSecretManager

P3-PLAN-v2 W20 DoD:
  - W20-1 SecretManager ABC + get/put/rotate 接口
  - W20-2 EnvSecretManager (Phase 1 默认, W20 复用)
  - W20-3 VaultSecretManager (Phase 3 新增)
      AppRole 认证 + KV v2 secret engine
      内存缓存 TTL (默认 5 分钟)

@note W20-4 rotation_due 事件通过 ObservabilityBus emit
@note W20-6 降级路径: --no-vault 时 fallback 到 EnvSecretManager
"""

from __future__ import annotations

import logging
import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class SecretError(Exception):
    """密钥管理错误基类"""


class SecretNotFoundError(SecretError):
    """secret 不存在"""


class SecretRotationDueError(SecretError):
    """secret 即将过期——应触发轮换"""


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


@dataclass
class SecretMetadata:
    """secret 元数据——含轮换时间"""

    key: str
    version: int = 1
    created_at: float = field(default_factory=time.time)
    expires_at: float | None = None  # None = 永不过期
    rotation_due_at: float | None = None  # 提前 N 天预警

    @property
    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        return time.time() >= self.expires_at

    @property
    def is_rotation_due(self) -> bool:
        if self.rotation_due_at is None:
            return False
        return time.time() >= self.rotation_due_at

    @property
    def seconds_to_rotation(self) -> float | None:
        if self.rotation_due_at is None:
            return None
        return self.rotation_due_at - time.time()


@dataclass
class Secret:
    """单条 secret"""

    value: str
    metadata: SecretMetadata


# ---------------------------------------------------------------------------
# 抽象
# ---------------------------------------------------------------------------


class SecretManager(ABC):
    """W20-① SecretManager ABC"""

    @abstractmethod
    async def get(self, key: str) -> Secret: ...

    @abstractmethod
    async def put(self, key: str, value: str, ttl_seconds: int | None = None) -> None: ...

    @abstractmethod
    async def delete(self, key: str) -> None: ...

    @abstractmethod
    async def rotate(self, key: str, new_value: str) -> Secret: ...

    @abstractmethod
    async def check_rotation_due(self) -> list[SecretMetadata]: ...

    @abstractmethod
    async def close(self) -> None: ...


# ---------------------------------------------------------------------------
# EnvSecretManager (Phase 1 默认实现——Phase 3 复用, 不破坏向后兼容)
# ---------------------------------------------------------------------------


class EnvSecretManager(SecretManager):
    """
    从环境变量读 secret——最简实现

    @note W20-6 降级路径: --no-vault 时使用此实现
    @note 不支持 put/delete/rotate——读 only
    """

    def __init__(self, env_prefix: str = "") -> None:
        """
        @param env_prefix  key 前缀, 如 "AGENT_SWARM_"
        """
        self.env_prefix = env_prefix

    def _full_key(self, key: str) -> str:
        return f"{self.env_prefix}{key}"

    async def get(self, key: str) -> Secret:
        env_key = self._full_key(key)
        value = os.environ.get(env_key)
        if value is None:
            raise SecretNotFoundError(f"secret {key!r} not found in env {env_key!r}")
        return Secret(
            value=value,
            metadata=SecretMetadata(key=key),
        )

    async def put(self, key: str, value: str, ttl_seconds: int | None = None) -> None:
        # Env 是 read-only——报 clear error
        raise NotImplementedError(
            "EnvSecretManager is read-only. "
            "Use VaultSecretManager for write/rotate.",
        )

    async def delete(self, key: str) -> None:
        raise NotImplementedError("EnvSecretManager is read-only")

    async def rotate(self, key: str, new_value: str) -> Secret:
        raise NotImplementedError(
            "EnvSecretManager is read-only. "
            "Rotation requires VaultSecretManager or restart with new env.",
        )

    async def check_rotation_due(self) -> list[SecretMetadata]:
        """Env 无元数据, 无可预警"""
        return []

    async def close(self) -> None:
        return None


# ---------------------------------------------------------------------------
# VaultSecretManager (Phase 3 新增)
# ---------------------------------------------------------------------------


@dataclass
class VaultConfig:
    """Vault 配置——W20-3"""

    url: str = "http://127.0.0.1:8200"
    role_id: str = ""
    secret_id: str = ""
    mount_point: str = "secret"  # KV v2 mount point
    cache_ttl_seconds: int = 300  # 5 分钟缓存
    rotation_warning_days: int = 7  # 提前 7 天预警
    timeout_seconds: float = 5.0
    # 测试用: 注入 vault client (避免真实 HTTP)
    vault_client: Any = None


@dataclass
class _CachedSecret:
    """内存缓存项"""

    secret: Secret
    cached_at: float

    @property
    def is_fresh(self) -> bool:
        return (time.time() - self.cached_at) < 300  # 默认 5 分钟


class VaultSecretManager(SecretManager):
    """
    W20-③ Vault 后端密钥管理

    @note AppRole 认证 + KV v2 secret engine
    @note 内存缓存 TTL (默认 5 分钟) + 轮换提前 7 天预警
    @note 测试用 fakeredis 风格的 mock vault client
    """

    def __init__(self, config: VaultConfig | None = None) -> None:
        self.config = config or VaultConfig()
        self._cache: dict[str, _CachedSecret] = {}
        self._vault: Any = None
        self._initialized = False

    async def _ensure_vault(self) -> None:
        if self._initialized:
            return
        if self.config.vault_client is None:
            try:
                import hvac
                self._vault = hvac.Client(
                    url=self.config.url,
                    timeout=self.config.timeout_seconds,
                )
                # AppRole auth
                if self.config.role_id and self.config.secret_id:
                    self._vault.auth.approle.login(
                        role_id=self.config.role_id,
                        secret_id=self.config.secret_id,
                    )
            except ImportError as e:
                raise SecretError(
                    "hvac library not installed. "
                    "Install with: pip install hvac>=2.0.0",
                ) from e
        else:
            self._vault = self.config.vault_client
        self._initialized = True

    async def _read_vault(self, key: str) -> Secret:
        """从 vault 读取——不走缓存"""
        await self._ensure_vault()
        # KV v2 路径: mount_point/data/{key}
        resp = self._vault.secrets.kv.v2.read_secret(
            path=key, mount_point=self.config.mount_point,
        )
        if not resp or "data" not in resp:
            raise SecretNotFoundError(f"vault secret {key!r} not found")
        data = resp["data"]["data"]
        # KV v2 必有 value 字段
        if "value" not in data:
            raise SecretError(f"vault secret {key!r} missing 'value' field")
        meta = resp["data"].get("metadata", {})
        sm = SecretMetadata(
            key=key,
            version=int(meta.get("version", 1)),
            created_at=float(meta.get("created_time", time.time())),
        )
        # 自定义元数据——ttl/rotation_due
        custom = data.get("__metadata__", {})
        if "ttl_seconds" in custom:
            sm.expires_at = sm.created_at + int(custom["ttl_seconds"])
            warn = self.config.rotation_warning_days * 86400
            sm.rotation_due_at = sm.expires_at - warn
        return Secret(value=data["value"], metadata=sm)

    async def get(self, key: str) -> Secret:
        # 缓存命中
        cached = self._cache.get(key)
        if cached and (time.time() - cached.cached_at) < self.config.cache_ttl_seconds:
            return cached.secret
        # 走 vault
        secret = await self._read_vault(key)
        self._cache[key] = _CachedSecret(secret=secret, cached_at=time.time())
        return secret

    async def put(
        self, key: str, value: str, ttl_seconds: int | None = None,
    ) -> None:
        await self._ensure_vault()
        data: dict[str, Any] = {"value": value}
        if ttl_seconds is not None:
            data["__metadata__"] = {"ttl_seconds": ttl_seconds}
        self._vault.secrets.kv.v2.create_or_update_secret(
            path=key, secret=data, mount_point=self.config.mount_point,
        )
        # 失效缓存
        self._cache.pop(key, None)

    async def delete(self, key: str) -> None:
        await self._ensure_vault()
        self._vault.secrets.kv.v2.delete_metadata_and_all_versions(
            path=key, mount_point=self.config.mount_point,
        )
        self._cache.pop(key, None)

    async def rotate(self, key: str, new_value: str) -> Secret:
        """轮换——读最新 → 写新 value"""
        await self._ensure_vault()
        # 读当前 metadata 保留 ttl
        try:
            current = await self._read_vault(key)
            ttl: int | None = None
            expires = current.metadata.expires_at
            created = current.metadata.created_at
            if current.metadata.rotation_due_at is not None:
                assert expires is not None
                ttl = int(expires - created)
        except SecretNotFoundError:
            ttl = None
        await self.put(key, new_value, ttl_seconds=ttl)
        # 失效缓存强制下次重读
        self._cache.pop(key, None)
        return await self.get(key)

    async def check_rotation_due(self) -> list[SecretMetadata]:
        """
        检查所有缓存中 secret 的轮换状态——W20-4

        @return 即将过期 (rotation_due_at <= now) 的 secret 列表
        """
        due: list[SecretMetadata] = []
        for cached in list(self._cache.values()):
            if cached.secret.metadata.is_rotation_due:
                due.append(cached.secret.metadata)
        return due

    async def close(self) -> None:
        if self._vault is not None and self.config.vault_client is None:
            import contextlib
            with contextlib.suppress(Exception):
                self._vault.adapter.close()
        self._initialized = False


__all__ = [
    "EnvSecretManager",
    "Secret",
    "SecretError",
    "SecretManager",
    "SecretMetadata",
    "SecretNotFoundError",
    "VaultConfig",
    "VaultSecretManager",
]
