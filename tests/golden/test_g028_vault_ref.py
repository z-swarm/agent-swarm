"""
@module tests.golden.test_g028_vault_ref
@brief  P5-W36c G-028 Golden Case — vault://path#field URI 端到端

@note 真实环境: agent-swarm Web UI 用 vault://path#field 走 VaultSecretManager,
      支持轮换 + field 提取 + JSON 解析 + 失败降级

@note 测试环境: fake VaultSecretManager 模拟 Vault KV v2 多 field 文档

覆盖:
  - Case 1: vault://path (无 field) → 直接用 value
  - Case 2: vault://path#field → JSON 提取
  - Case 3: rotate 后 field 值变化 → cache 失效
  - Case 4: Vault 不可用 + cache 命中 → 降级 (P0 防御深度)
"""

from __future__ import annotations

import json

import pytest

from agent_swarm.security.secret_manager import (
    Secret,
    SecretManager,
    SecretMetadata,
)
from agent_swarm.web.auth import (
    JWTConfig,
    JWTError,
    JWTIssuer,
    parse_secret_ref,
)

# ---------------------------------------------------------------------------
# Fake VaultSecretManager (KV v2 风格: JSON 文档)
# ---------------------------------------------------------------------------


class _FakeVaultKV(SecretManager):
    """
    @brief G-028 测试用 Vault KV v2 fake

    特性:
      - put 每次 version+1
      - value 是 JSON 字符串 (模拟多 field 文档)
      - fail_get: 让下次 get 抛 (模拟 Vault 宕机)
    """

    def __init__(self) -> None:
        self._store: dict[str, Secret] = {}
        self._version: dict[str, int] = {}
        self.fail_get = False

    async def get(self, key: str) -> Secret:
        if self.fail_get:
            self.fail_get = False
            raise RuntimeError("simulated vault outage")
        if key not in self._store:
            from agent_swarm.security.secret_manager import SecretNotFoundError
            raise SecretNotFoundError(f"vault: {key!r} not found")
        return self._store[key]

    async def put(self, key: str, value: str, ttl_seconds: int | None = None) -> None:
        self._version[key] = self._version.get(key, 0) + 1
        self._store[key] = Secret(
            value=value,
            metadata=SecretMetadata(key=key, version=self._version[key]),
        )

    async def delete(self, key: str) -> None:
        self._store.pop(key, None)
        self._version.pop(key, None)

    async def rotate(self, key: str, new_value: str) -> Secret:
        await self.put(key, new_value)
        return await self.get(key)

    async def check_rotation_due(self) -> list[SecretMetadata]:
        return []

    async def close(self) -> None:
        return None


# ---------------------------------------------------------------------------
# Case 1: vault://path (无 field) → 直接用 value
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_g028_vault_no_field() -> None:
    """vault://path (无 #field) → value 直接当 secret"""
    vault = _FakeVaultKV()
    await vault.put("web/jwt", "plain-secret-no-json")
    iss = JWTIssuer(
        JWTConfig(secret_ref="vault://web/jwt", secret_manager=vault),
    )
    sec = await iss.resolve_secret()
    assert sec == b"plain-secret-no-json"


# ---------------------------------------------------------------------------
# Case 2: vault://path#field → JSON 提取 field
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_g028_vault_with_field_extracts_json() -> None:
    """vault://path#field → JSON 文档中提取指定 field"""
    vault = _FakeVaultKV()
    doc = json.dumps({
        "current": "jwt-rotation-v1",
        "previous": "jwt-rotation-v0",
        "metadata": {
            "rotated_at": "2026-06-24",
            "rotated_by": "ops",
        },
    })
    await vault.put("web/jwt-secret", doc)
    iss = JWTIssuer(
        JWTConfig(secret_ref="vault://web/jwt-secret#current", secret_manager=vault),
    )
    sec = await iss.resolve_secret()
    assert sec == b"jwt-rotation-v1"
    # decode 用 cache 的 secret 验证 token
    token = iss.encode("alice", {"role": "admin"})
    claims = iss.decode(token)
    assert claims["sub"] == "alice"
    assert claims["role"] == "admin"


# ---------------------------------------------------------------------------
# Case 3: rotate 后 field 值变化 → cache 失效
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_g028_vault_rotation_invalidates_cache() -> None:
    """vault:// rotate → version 变化 → cache 失效 → 拿到新 field"""
    vault = _FakeVaultKV()
    doc_v1 = json.dumps({"current": "secret-v1"})
    await vault.put("web/jwt", doc_v1)
    iss = JWTIssuer(
        JWTConfig(secret_ref="vault://web/jwt#current", secret_manager=vault),
    )
    # 初始
    sec1 = await iss.resolve_secret()
    assert sec1 == b"secret-v1"
    assert iss._cached_version == 1
    # 轮换
    doc_v2 = json.dumps({"current": "secret-v2"})
    await vault.rotate("web/jwt", doc_v2)
    # 触发 cache 刷新
    sec2 = await iss.resolve_secret()
    assert sec2 == b"secret-v2"
    assert iss._cached_version == 2


# ---------------------------------------------------------------------------
# Case 4: Vault 不可用 + cache 命中 → 降级
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_g028_vault_unavailable_cache_fallback() -> None:
    """Vault 宕机 + cache 命中 → 降级用 cache (P0 防御深度)"""
    vault = _FakeVaultKV()
    doc = json.dumps({"current": "cached-secret"})
    await vault.put("web/jwt", doc)
    iss = JWTIssuer(
        JWTConfig(secret_ref="vault://web/jwt#current", secret_manager=vault),
    )
    # 初始化 cache
    sec1 = await iss.resolve_secret()
    assert sec1 == b"cached-secret"
    # 注入 Vault 故障
    vault.fail_get = True
    # 降级: cache 命中, 继续用
    sec2 = await iss.resolve_secret()
    assert sec2 == b"cached-secret"
    # decode 仍能用 (走 cache)
    token = iss.encode("bob")
    assert iss.decode(token)["sub"] == "bob"


# ---------------------------------------------------------------------------
# Bonus: G-028 端到端 (parse + resolve + encode + decode + rotate)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_g028_full_lifecycle() -> None:
    """G-028 完整端到端: parse → resolve → encode → decode → rotate → 失效"""
    # 1) parse_secret_ref
    ref = parse_secret_ref("vault://web/jwt-secret#current")
    assert ref.kind == "vault"
    assert ref.value == "web/jwt-secret"
    assert ref.field == "current"
    # 2) 部署
    vault = _FakeVaultKV()
    doc = json.dumps({"current": "phase-1-secret", "previous": "phase-0-secret"})
    await vault.put("web/jwt-secret", doc)
    iss = JWTIssuer(
        JWTConfig(secret_ref="vault://web/jwt-secret#current", secret_manager=vault),
    )
    # 3) Phase A: 用 v1 签发
    await iss.resolve_secret()
    token_v1 = iss.encode("alice")
    assert iss.decode(token_v1)["sub"] == "alice"
    # 4) Phase B: rotate
    doc_v2 = json.dumps({"current": "phase-2-secret", "previous": "phase-1-secret"})
    await vault.rotate("web/jwt-secret", doc_v2)
    # 5) Phase C: 旧 token 在 cache TTL 内仍 verify
    assert iss.decode(token_v1)["sub"] == "alice"
    # 6) 触发 cache 更新
    await iss.resolve_secret()
    # 7) Phase D: 旧 token 失效
    with pytest.raises(JWTError, match="signature"):
        iss.decode(token_v1)
    # 8) 新 token 用 v2
    token_v2 = iss.encode("bob", {"role": "user"})
    claims = iss.decode(token_v2)
    assert claims["sub"] == "bob"
    assert claims["role"] == "user"
