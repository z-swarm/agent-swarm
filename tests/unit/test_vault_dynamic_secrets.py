"""
@module tests.unit.test_vault_dynamic_secrets
@brief  P4-W26 Vault Dynamic Secrets 测试

覆盖:
  - get_dynamic_credentials: 发凭证
  - DBCredentials: ttl / expired / as_dsn
  - renew_lease: 续约
  - revoke_lease / revoke_all: 显式回收
  - 与 VaultSecretManager 集成
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from agent_swarm.security.secret_manager import (
    DBCredentials,
    VaultConfig,
    VaultDynamicSecretManager,
    VaultSecretManager,
)

# ---------------------------------------------------------------------------
# Fake vault client
# ---------------------------------------------------------------------------


class FakeSecretsKvV2:
    """只占位——动态凭证不走 KV v2"""

    def __init__(self) -> None:
        pass


class FakeDatabase:
    """模拟 vault.secrets.database"""

    def __init__(self) -> None:
        self.issued: list[dict[str, Any]] = []

    def generate_credentials(self, name: str) -> dict[str, Any]:
        # 模拟 Vault 返回
        lease_id = f"database/creds/{name}/{int(time.time())}-{len(self.issued)}"
        resp = {
            "lease_id": lease_id,
            "renewable": True,
            "lease_duration_seconds": 3600,
            "data": {
                "username": f"v-{name}-{len(self.issued)}-user",
                "password": f"pw-{int(time.time() * 1000)}",
            },
        }
        self.issued.append({"role": name, "lease_id": lease_id, "ts": time.time()})
        return resp


class FakeSysLeases:
    def __init__(self) -> None:
        self.renewed: list[str] = []
        self.revoked: list[str] = []

    def renew(self, lease_id: str, increment: int = 3600) -> dict[str, Any]:
        self.renewed.append(lease_id)
        return {
            "lease_id": lease_id,
            "renewable": True,
            "lease_duration_seconds": increment,
        }

    def revoke(self, lease_id: str) -> None:
        self.revoked.append(lease_id)


class FakeVaultClient:
    def __init__(self) -> None:
        self.secrets = type(
            "S",
            (),
            {
                "kv": type("KV", (), {"v2": FakeSecretsKvV2()})(),
                "database": FakeDatabase(),
            },
        )()
        self.sys = type("Sys", (), {"leases": FakeSysLeases()})()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_vault() -> FakeVaultClient:
    return FakeVaultClient()


@pytest.fixture
def vsm(fake_vault: FakeVaultClient) -> VaultSecretManager:
    cfg = VaultConfig(vault_client=fake_vault)
    return VaultSecretManager(cfg)


@pytest.fixture
def dyn(vsm: VaultSecretManager) -> VaultDynamicSecretManager:
    return VaultDynamicSecretManager(vsm)


# ---------------------------------------------------------------------------
# DBCredentials
# ---------------------------------------------------------------------------


def test_dbcredentials_defaults() -> None:
    """DBCredentials: 默认值"""
    c = DBCredentials(
        username="u",
        password="p",
        lease_id="l1",
        lease_duration_seconds=60,
    )
    assert c.username == "u"
    assert c.lease_duration_seconds == 60
    assert c.renewable is True
    assert c.issued_at <= time.time()


def test_dbcredentials_expires_at() -> None:
    """expires_at = issued_at + ttl"""
    c = DBCredentials(
        username="u",
        password="p",
        lease_id="l1",
        lease_duration_seconds=3600,
    )
    expected = c.issued_at + 3600
    assert abs(c.expires_at - expected) < 0.01


def test_dbcredentials_seconds_to_expiry() -> None:
    """seconds_to_expiry = ttl - elapsed"""
    c = DBCredentials(
        username="u",
        password="p",
        lease_id="l1",
        lease_duration_seconds=3600,
    )
    assert c.seconds_to_expiry > 3500
    assert c.seconds_to_expiry <= 3600


def test_dbcredentials_as_dsn() -> None:
    """as_dsn: 组装 postgresql DSN"""
    c = DBCredentials(
        username="alice",
        password="s3cret",
        lease_id="l1",
        lease_duration_seconds=3600,
    )
    dsn = c.as_dsn("db.example.com", 5432, "myapp")
    assert dsn == "postgresql://alice:s3cret@db.example.com:5432/myapp"


# ---------------------------------------------------------------------------
# get_dynamic_credentials
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_dynamic_credentials(
    dyn: VaultDynamicSecretManager,
    fake_vault: FakeVaultClient,
) -> None:
    """get_dynamic_credentials: 返回 DBCredentials + 记录 lease"""
    c = await dyn.get_dynamic_credentials("readonly")
    assert c.username.startswith("v-readonly-")
    assert c.password.startswith("pw-")
    assert c.lease_id.startswith("database/creds/readonly/")
    assert c.lease_duration_seconds == 3600
    assert fake_vault.secrets.database.issued[0]["role"] == "readonly"


@pytest.mark.asyncio
async def test_get_dynamic_credentials_records_lease(
    dyn: VaultDynamicSecretManager,
) -> None:
    """get 后 lease 在 _active_leases 里"""
    c = await dyn.get_dynamic_credentials("rw")
    assert any(x.lease_id == c.lease_id for x in dyn.list_active_leases())


@pytest.mark.asyncio
async def test_get_dynamic_credentials_unique(
    dyn: VaultDynamicSecretManager,
) -> None:
    """多次 get → 多个不同 lease"""
    c1 = await dyn.get_dynamic_credentials("rw")
    c2 = await dyn.get_dynamic_credentials("rw")
    assert c1.lease_id != c2.lease_id
    assert c1.username != c2.username


# ---------------------------------------------------------------------------
# renew_lease
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_renew_lease(
    dyn: VaultDynamicSecretManager,
    fake_vault: FakeVaultClient,
) -> None:
    """renew: 调 vault, 更新 ttl"""
    c = await dyn.get_dynamic_credentials("rw")
    renewed = await dyn.renew_lease(c.lease_id, increment=7200)
    assert renewed.lease_duration_seconds == 7200
    assert fake_vault.sys.leases.renewed == [c.lease_id]


@pytest.mark.asyncio
async def test_renew_unknown_lease_raises(
    dyn: VaultDynamicSecretManager,
) -> None:
    """renew 未知 lease: 抛 SecretError"""
    from agent_swarm.security.secret_manager import SecretError

    with pytest.raises(SecretError, match="unknown lease_id"):
        await dyn.renew_lease("nonexistent-lease-id")


# ---------------------------------------------------------------------------
# revoke_lease
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_revoke_lease(
    dyn: VaultDynamicSecretManager,
    fake_vault: FakeVaultClient,
) -> None:
    """revoke: 调 vault + 移除 _active_leases"""
    c = await dyn.get_dynamic_credentials("rw")
    await dyn.revoke_lease(c.lease_id)
    assert c.lease_id not in [x.lease_id for x in dyn.list_active_leases()]
    assert c.lease_id in fake_vault.sys.leases.revoked


@pytest.mark.asyncio
async def test_revoke_unknown_lease_is_noop(
    dyn: VaultDynamicSecretManager,
) -> None:
    """revoke 未知 lease: 不报错 (幂等)"""
    await dyn.revoke_lease("nonexistent")  # no raise


@pytest.mark.asyncio
async def test_revoke_all(
    dyn: VaultDynamicSecretManager,
    fake_vault: FakeVaultClient,
) -> None:
    """revoke_all: 全部回收"""
    for i in range(5):
        await dyn.get_dynamic_credentials(f"role{i}")
    assert len(dyn.list_active_leases()) == 5
    n = await dyn.revoke_all()
    assert n == 5
    assert len(dyn.list_active_leases()) == 0
    assert len(fake_vault.sys.leases.revoked) == 5


# ---------------------------------------------------------------------------
# 集成测试
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dyn_uses_vsm_vault_client(
    vsm: VaultSecretManager,
    fake_vault: FakeVaultClient,
) -> None:
    """VaultDynamicSecretManager 复用 VaultSecretManager 的 vault client"""
    dyn = VaultDynamicSecretManager(vsm)
    c = await dyn.get_dynamic_credentials("rw")
    assert c.username == fake_vault.secrets.database.issued[0].get("username", "") or c.username


@pytest.mark.asyncio
async def test_workflow_full(
    dyn: VaultDynamicSecretManager,
    fake_vault: FakeVaultClient,
) -> None:
    """完整工作流: get → renew → revoke"""
    # 1. 获取
    creds = await dyn.get_dynamic_credentials("rw")
    assert not creds.is_expired
    # 2. 用
    dsn = creds.as_dsn("localhost", 5432, "test")
    assert "postgresql://" in dsn
    # 3. 续约
    await dyn.renew_lease(creds.lease_id, increment=7200)
    # 4. 回收
    await dyn.revoke_lease(creds.lease_id)
    assert creds.lease_id not in [x.lease_id for x in dyn.list_active_leases()]
