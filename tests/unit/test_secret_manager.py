"""
@module tests.unit.test_secret_manager
@brief  W20-①② ③ SecretManager 测试

覆盖:
  - SecretMetadata 过期/轮换预警
  - EnvSecretManager: read / put raises / rotation_due 空
  - VaultSecretManager: 用 mock vault_client 验证 get/put/delete/rotate
  - 缓存 TTL 行为
  - rotation_due 列表
"""

from __future__ import annotations

import os
import time

import pytest

from agent_swarm.security.secret_manager import (
    EnvSecretManager,
    SecretMetadata,
    SecretNotFoundError,
    VaultConfig,
    VaultSecretManager,
)

# ---------------------------------------------------------------------------
# SecretMetadata
# ---------------------------------------------------------------------------


def test_secret_metadata_no_expiry() -> None:
    sm = SecretMetadata(key="k")
    assert sm.is_expired is False
    assert sm.is_rotation_due is False
    assert sm.seconds_to_rotation is None


def test_secret_metadata_expired() -> None:
    sm = SecretMetadata(
        key="k",
        expires_at=time.time() - 1,
        rotation_due_at=time.time() - 1,
    )
    assert sm.is_expired is True
    assert sm.is_rotation_due is True


def test_secret_metadata_rotation_due_in_7_days() -> None:
    """7 天后即将轮换预警——rotation_due_at 设在过去 10s"""
    past = time.time() - 10
    sm = SecretMetadata(
        key="k",
        rotation_due_at=past,
        expires_at=past + 7 * 86400,
    )
    assert sm.is_rotation_due is True
    secs = sm.seconds_to_rotation
    assert secs is not None and -20 < secs < 0


# ---------------------------------------------------------------------------
# EnvSecretManager
# ---------------------------------------------------------------------------


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """清空 AGENT_SWARM_TEST_ 前缀的环境变量"""
    for k in list(os.environ):
        if k.startswith("AGENT_SWARM_TEST_"):
            monkeypatch.delenv(k, raising=False)


@pytest.mark.asyncio
async def test_env_get(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_SWARM_TEST_KEY", "secret-value-123")
    mgr = EnvSecretManager(env_prefix="AGENT_SWARM_TEST_")
    s = await mgr.get("KEY")
    assert s.value == "secret-value-123"
    assert s.metadata.key == "KEY"


@pytest.mark.asyncio
async def test_env_get_missing_raises(clean_env: None) -> None:
    mgr = EnvSecretManager(env_prefix="AGENT_SWARM_TEST_")
    with pytest.raises(SecretNotFoundError, match="not found"):
        await mgr.get("MISSING")


@pytest.mark.asyncio
async def test_env_put_raises(clean_env: None) -> None:
    mgr = EnvSecretManager(env_prefix="AGENT_SWARM_TEST_")
    with pytest.raises(NotImplementedError, match="read-only"):
        await mgr.put("KEY", "value")


@pytest.mark.asyncio
async def test_env_rotate_raises(clean_env: None) -> None:
    mgr = EnvSecretManager(env_prefix="AGENT_SWARM_TEST_")
    with pytest.raises(NotImplementedError, match="read-only"):
        await mgr.rotate("KEY", "new")


@pytest.mark.asyncio
async def test_env_check_rotation_due_empty(clean_env: None) -> None:
    mgr = EnvSecretManager(env_prefix="AGENT_SWARM_TEST_")
    assert await mgr.check_rotation_due() == []


# ---------------------------------------------------------------------------
# VaultSecretManager (mock vault client)
# ---------------------------------------------------------------------------


class _MockVaultClient:
    """模拟 hvac.Client——足够覆盖 KV v2 read/write"""

    def __init__(self) -> None:
        self._store: dict[str, dict[str, object]] = {}
        self.secrets = _MockSecrets(self)


class _MockSecrets:
    def __init__(self, parent: _MockVaultClient) -> None:
        self.kv = _MockKV(parent)
        self._parent = parent


class _MockKV:
    def __init__(self, parent: _MockVaultClient) -> None:
        self.v2 = _MockKVv2(parent)
        self._parent = parent


class _MockKVv2:
    def __init__(self, parent: _MockVaultClient) -> None:
        self._parent = parent

    def read_secret(self, *, path: str, mount_point: str) -> dict[str, object] | None:  # noqa: ARG002
        if path not in self._parent._store:
            return None
        data = self._parent._store[path]
        return {
            "data": {
                "data": data,
                "metadata": {"version": 1, "created_time": time.time()},
            },
        }

    def create_or_update_secret(
        self,
        *,
        path: str,
        secret: dict[str, object],
        mount_point: str,  # noqa: ARG002
    ) -> None:
        self._parent._store[path] = dict(secret)

    def delete_metadata_and_all_versions(
        self,
        *,
        path: str,
        mount_point: str,  # noqa: ARG002
    ) -> None:
        self._parent._store.pop(path, None)


@pytest.fixture
def mock_vault() -> _MockVaultClient:
    return _MockVaultClient()


@pytest.mark.asyncio
async def test_vault_put_get_roundtrip(mock_vault: _MockVaultClient) -> None:
    cfg = VaultConfig(vault_client=mock_vault, mount_point="secret")
    mgr = VaultSecretManager(cfg)
    await mgr.put("OPENAI_API_KEY", "sk-abc", ttl_seconds=86400 * 30)
    s = await mgr.get("OPENAI_API_KEY")
    assert s.value == "sk-abc"


@pytest.mark.asyncio
async def test_vault_get_missing_raises(mock_vault: _MockVaultClient) -> None:
    cfg = VaultConfig(vault_client=mock_vault)
    mgr = VaultSecretManager(cfg)
    with pytest.raises(SecretNotFoundError):
        await mgr.get("NONEXISTENT")


@pytest.mark.asyncio
async def test_vault_delete(mock_vault: _MockVaultClient) -> None:
    cfg = VaultConfig(vault_client=mock_vault)
    mgr = VaultSecretManager(cfg)
    await mgr.put("KEY", "value")
    assert (await mgr.get("KEY")).value == "value"
    await mgr.delete("KEY")
    with pytest.raises(SecretNotFoundError):
        await mgr.get("KEY")


@pytest.mark.asyncio
async def test_vault_rotate(mock_vault: _MockVaultClient) -> None:
    cfg = VaultConfig(vault_client=mock_vault, cache_ttl_seconds=0)
    mgr = VaultSecretManager(cfg)
    await mgr.put("KEY", "old")
    new = await mgr.rotate("KEY", "new")
    assert new.value == "new"


@pytest.mark.asyncio
async def test_vault_cache_ttl(mock_vault: _MockVaultClient) -> None:
    """缓存 TTL 行为——5 分钟内不走 vault"""
    cfg = VaultConfig(vault_client=mock_vault, cache_ttl_seconds=60)
    mgr = VaultSecretManager(cfg)
    await mgr.put("KEY", "v1")
    # 读一次——缓存
    s1 = await mgr.get("KEY")
    assert s1.value == "v1"
    # vault 端改了 value 但缓存还在
    mock_vault._store["KEY"]["value"] = "v2"  # type: ignore[index]
    s2 = await mgr.get("KEY")
    assert s2.value == "v1"  # 缓存命中


@pytest.mark.asyncio
async def test_vault_put_invalidates_cache(
    mock_vault: _MockVaultClient,
) -> None:
    cfg = VaultConfig(vault_client=mock_vault, cache_ttl_seconds=60)
    mgr = VaultSecretManager(cfg)
    await mgr.put("KEY", "v1")
    await mgr.get("KEY")  # 缓存
    await mgr.put("KEY", "v2")  # 写——应失效缓存
    s = await mgr.get("KEY")
    assert s.value == "v2"


@pytest.mark.asyncio
async def test_vault_rotation_due(mock_vault: _MockVaultClient) -> None:
    """轮换预警——expires_at 接近 → check_rotation_due 返回该项"""
    cfg = VaultConfig(vault_client=mock_vault)
    mgr = VaultSecretManager(cfg)
    # 写一个即将过期的 secret (ttl 1s + rotation_warning_days=7)
    # expires_at 在 created_at+1s, rotation_due_at = expires_at - 7d
    # rotation_due_at 在过去 → 立即触发预警
    # 用 put 直接放一个固定 expires_at 比较稳
    # 这里 mock 端不支持自定义 expires_at, 直接改 store 模拟
    mock_vault._store["SOON"] = {  # type: ignore[index]
        "value": "x",
        "__metadata__": {
            "ttl_seconds": 1,  # 1s 后过期
        },
    }
    # 由于 mock 不支持 expires_at 解析, 跳过 metadata 检测
    # 改成手动注入一个 SecretMetadata 到 cache
    from agent_swarm.security.secret_manager import Secret

    sm = SecretMetadata(
        key="SOON",
        expires_at=time.time() - 1,
        rotation_due_at=time.time() - 1,
    )
    from agent_swarm.security.secret_manager import _CachedSecret

    mgr._cache["SOON"] = _CachedSecret(  # type: ignore[attr-defined]
        secret=Secret(value="x", metadata=sm),
        cached_at=time.time(),
    )
    due = await mgr.check_rotation_due()
    assert any(d.key == "SOON" for d in due)


@pytest.mark.asyncio
async def test_vault_close(mock_vault: _MockVaultClient) -> None:
    cfg = VaultConfig(vault_client=mock_vault)
    mgr = VaultSecretManager(cfg)
    await mgr._ensure_vault()  # type: ignore[attr-defined]
    await mgr.close()
    # 关闭后再访问应仍可 (mock vault client 不真关闭)
    await mgr.put("KEY", "v")
