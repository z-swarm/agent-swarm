"""
@module tests.golden.test_g026_jwt_rotation
@brief  P5-W36a G-026 Golden Case — JWT secret 轮换不重启端到端

@note 真实环境: agent-swarm Web UI 运行中, 运维调 Vault rotate JWT secret,
      Web UI 无需重启, 旧 token 在 cache TTL 内仍 verify, 新 token 用新 secret

@note 测试环境: 用 FakeSecretManager (in-memory) 模拟 Vault KV v2,
      4 phases 端到端走通: 签发 v1 / rotate / 旧 token verify / 新 token verify
"""

from __future__ import annotations

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
)

# ---------------------------------------------------------------------------
# Fake Vault-like SecretManager
# ---------------------------------------------------------------------------


class _FakeVault(SecretManager):
    """
    @brief G-026 测试用 Vault fake (KV v2 风格)

    特性:
      - put 每次 version+1
      - rotate(key, new) 等价 put(key, new) + 返回新 Secret
    """

    def __init__(self) -> None:
        self._store: dict[str, Secret] = {}
        self._version_counter: dict[str, int] = {}
        self.get_count = 0

    async def get(self, key: str) -> Secret:
        self.get_count += 1
        if key not in self._store:
            from agent_swarm.security.secret_manager import SecretNotFoundError
            raise SecretNotFoundError(f"vault: {key!r} not found")
        return self._store[key]

    async def put(self, key: str, value: str, ttl_seconds: int | None = None) -> None:
        self._version_counter[key] = self._version_counter.get(key, 0) + 1
        self._store[key] = Secret(
            value=value,
            metadata=SecretMetadata(
                key=key,
                version=self._version_counter[key],
            ),
        )

    async def delete(self, key: str) -> None:
        self._store.pop(key, None)
        self._version_counter.pop(key, None)

    async def rotate(self, key: str, new_value: str) -> Secret:
        await self.put(key, new_value)
        return await self.get(key)

    async def check_rotation_due(self) -> list[SecretMetadata]:
        return []

    async def close(self) -> None:
        return None


# ---------------------------------------------------------------------------
# G-026 Phase A: 用 secret_v1 签发 token → verify OK
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_g026_phase_a_sign_and_verify_v1() -> None:
    """G-026 Phase A: 初始 secret 签发 + 验证通过"""
    vault = _FakeVault()
    await vault.put("web/jwt-secret", "initial-secret-v1")
    iss = JWTIssuer(JWTConfig(secret_ref="secret://web/jwt-secret", secret_manager=vault))
    await iss.resolve_secret()
    token = iss.encode("alice", {"role": "admin"})
    claims = iss.decode(token)
    assert claims["sub"] == "alice"
    assert claims["role"] == "admin"
    assert iss._cached_version == 1


# ---------------------------------------------------------------------------
# G-026 Phase B: rotate 到 secret_v2
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_g026_phase_b_rotate() -> None:
    """G-026 Phase B: rotate 后 version+1, secret 更新"""
    vault = _FakeVault()
    await vault.put("web/jwt-secret", "secret-v1")
    iss = JWTIssuer(JWTConfig(secret_ref="secret://web/jwt-secret", secret_manager=vault))
    await iss.resolve_secret()
    assert iss._cached_version == 1
    # rotate
    await vault.rotate("web/jwt-secret", "secret-v2")
    # resolve 拉到 v2
    await iss.resolve_secret()
    assert iss._cached_version == 2
    sec = await iss.resolve_secret()
    assert sec == b"secret-v2"


# ---------------------------------------------------------------------------
# G-026 Phase C: 旧 token 在 cache TTL 内仍 verify, 触发更新后失效
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_g026_phase_c_old_token_cache_ttl() -> None:
    """
    G-026 Phase C: cache TTL 内旧 token 仍 verify, 触发 resolve_secret 后失效

    这是核心 SLA 场景:
      - 运维在 t0 rotate secret
      - Web UI 已有在用 token (t0 之前签发)
      - 旧 token 在 cache TTL (默认 5min) 内仍 verify
      - 触发 resolve_secret 后 (轮换检测 / 周期任务), 旧 token 失效
    """
    vault = _FakeVault()
    await vault.put("web/jwt-secret", "secret-v1")
    iss = JWTIssuer(JWTConfig(secret_ref="secret://web/jwt-secret", secret_manager=vault))
    await iss.resolve_secret()
    # 用 v1 签发 token
    token_v1 = iss.encode("alice")
    assert iss.decode(token_v1)["sub"] == "alice"
    # rotate 到 v2
    await vault.rotate("web/jwt-secret", "secret-v2")
    # Phase C-1: 不主动 resolve → cache 仍 v1 → 旧 token 仍 verify
    assert iss.decode(token_v1)["sub"] == "alice"
    # Phase C-2: 主动 resolve → cache 拉到 v2 → 旧 token 失效
    await iss.resolve_secret()
    with pytest.raises(JWTError, match="signature"):
        iss.decode(token_v1)


# ---------------------------------------------------------------------------
# G-026 Phase D: 新 token 用新 secret verify OK
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_g026_phase_d_new_token_with_v2() -> None:
    """G-026 Phase D: rotate 后, 新签发 token 用新 secret verify OK"""
    vault = _FakeVault()
    await vault.put("web/jwt-secret", "secret-v1")
    iss = JWTIssuer(JWTConfig(secret_ref="secret://web/jwt-secret", secret_manager=vault))
    await iss.resolve_secret()
    # rotate
    await vault.rotate("web/jwt-secret", "secret-v2")
    # 触发 cache 刷新
    await iss.resolve_secret()
    # 签发新 token
    token_v2 = iss.encode("bob", {"role": "operator"})
    claims = iss.decode(token_v2)
    assert claims["sub"] == "bob"
    assert claims["role"] == "operator"
    # 旧 token 不可 verify
    token_v1 = JWTIssuer(JWTConfig(secret="secret-v1")).encode("alice")
    with pytest.raises(JWTError, match="signature"):
        iss.decode(token_v1)


# ---------------------------------------------------------------------------
# G-026 端到端串联: A → B → C → D 一气呵成
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_g026_full_lifecycle() -> None:
    """
    G-026 完整端到端: Phase A 签 v1 → Phase B rotate → Phase C 旧 token TTL → Phase D 新 token v2

    模拟真实运维场景:
      1. 部署时 put secret-v1
      2. 业务签发 token 给用户 alice (Phase A)
      3. 90 天后运维 rotate secret (Phase B)
      4. 用户 alice 的旧 token 短期仍可用 (Phase C)
      5. 用户 bob 重新登录拿新 token (Phase D)
    """
    vault = _FakeVault()
    # 部署
    await vault.put("web/jwt-secret", "rotation-secret-v1")
    iss = JWTIssuer(JWTConfig(secret_ref="secret://web/jwt-secret", secret_manager=vault))
    await iss.resolve_secret()
    # Phase A: alice 登录
    token_alice_v1 = iss.encode("alice")
    assert iss.decode(token_alice_v1)["sub"] == "alice"
    # Phase B: 运维 rotate
    await vault.rotate("web/jwt-secret", "rotation-secret-v2")
    # Phase C: alice 的旧 token 短期仍可用 (cache 没刷)
    assert iss.decode(token_alice_v1)["sub"] == "alice"
    # Phase C-2: 定时任务触发 cache 刷新 (模拟 lifespan 周期)
    await iss.resolve_secret()
    # 旧 token 失效
    with pytest.raises(JWTError, match="signature"):
        iss.decode(token_alice_v1)
    # Phase D: bob 登录拿新 token
    token_bob_v2 = iss.encode("bob", {"role": "user"})
    claims = iss.decode(token_bob_v2)
    assert claims["sub"] == "bob"
    assert claims["role"] == "user"
    # 终态: cache 是 v2, version=2
    assert iss._cached_version == 2
    sec = await iss.resolve_secret()
    assert sec == b"rotation-secret-v2"
