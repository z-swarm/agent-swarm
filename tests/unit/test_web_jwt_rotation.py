"""
@module tests.unit.test_web_jwt_rotation
@brief  P5-W36a 轮换 cache + 降级路径单测 (≥4 cases)

覆盖:
  - resolve_secret: 首次调用走 SecretManager.get, 后续命中 cache
  - SecretMetadata.version 变化 → cache 失效 → 重读
  - SecretManager.get 失败时降级: cache 命中 → 继续用; cache miss → JWTError
  - invalidate_cache 强制下次 resolve_secret 重读
  - 完整轮换流程: sign v1 → rotate → sign v2 → v1 token 在 TTL 内 verify / v2 永远 verify
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
# Fake SecretManager (in-memory + 轮换 + 故障注入)
# ---------------------------------------------------------------------------


class _FakeSecretManager(SecretManager):
    """
    @brief W36a 测试用 SecretManager fake

    特性:
      - in-memory dict 存储
      - put 每次 version+1
      - fail_get: 让下次 get 抛 RuntimeError (模拟 Vault 临时不可用)
    """

    def __init__(self) -> None:
        self._store: dict[str, Secret] = {}
        self._version = 0
        self.fail_get = False
        self.get_count = 0  # 监控 cache 命中

    async def get(self, key: str) -> Secret:
        self.get_count += 1
        if self.fail_get:
            self.fail_get = False
            raise RuntimeError("simulated vault outage")
        if key not in self._store:
            from agent_swarm.security.secret_manager import SecretNotFoundError
            raise SecretNotFoundError(f"key {key!r} not found")
        return self._store[key]

    async def put(self, key: str, value: str, ttl_seconds: int | None = None) -> None:
        self._version += 1
        self._store[key] = Secret(
            value=value,
            metadata=SecretMetadata(key=key, version=self._version),
        )

    async def delete(self, key: str) -> None:
        self._store.pop(key, None)

    async def rotate(self, key: str, new_value: str) -> Secret:
        await self.put(key, new_value)
        return await self.get(key)

    async def check_rotation_due(self) -> list[SecretMetadata]:
        return []

    async def close(self) -> None:
        return None


# ---------------------------------------------------------------------------
# D4-1: 首次 resolve_secret 走 SecretManager, 后续命中 cache
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_secret_first_call_calls_manager() -> None:
    """resolve_secret 每次都调 SecretManager.get (always-fresh 语义)
    cache 只服务于 decode 的 sync 路径, 不被 resolve_secret 短路"""
    mgr = _FakeSecretManager()
    await mgr.put("web/jwt", "secret-v1")
    config = JWTConfig(secret_ref="secret://web/jwt", secret_manager=mgr)
    iss = JWTIssuer(config)
    # 首次
    sec1 = await iss.resolve_secret()
    assert sec1 == b"secret-v1"
    assert mgr.get_count == 1
    # 第二次: 同样调 get (version 未变, 但 resolve_secret 是 always-fresh)
    sec2 = await iss.resolve_secret()
    assert sec2 == b"secret-v1"
    assert mgr.get_count == 2
    # decode 走 cache (sync), 不增加 get_count
    token = iss.encode("alice")
    claims = iss.decode(token)
    assert claims["sub"] == "alice"
    assert mgr.get_count == 2  # 没变 → decode 走 cache


# ---------------------------------------------------------------------------
# D4-2: SecretMetadata.version 变化 → cache 失效 → 重读
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_secret_version_change_invalidates_cache() -> None:
    """version 变化 → cache 自动更新"""
    mgr = _FakeSecretManager()
    await mgr.put("web/jwt", "secret-v1")
    iss = JWTIssuer(JWTConfig(secret_ref="secret://web/jwt", secret_manager=mgr))
    # 首次
    sec1 = await iss.resolve_secret()
    assert sec1 == b"secret-v1"
    assert iss._cached_version == 1
    # rotate: version → 2 (rotate 内部 put + get, get_count=2)
    await mgr.rotate("web/jwt", "secret-v2")
    # 下次 resolve 拿到 v2, cache 更新
    sec2 = await iss.resolve_secret()
    assert sec2 == b"secret-v2"
    assert iss._cached_version == 2
    # get_count = 1 (首次) + 1 (rotate 内 get) + 1 (resolve v2) = 3
    assert mgr.get_count == 3


# ---------------------------------------------------------------------------
# D4-3: SecretManager.get 失败时降级 — cache 命中 → 继续用
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_secret_get_failure_with_cache_uses_cache() -> None:
    """SecretManager.get 失败时, cache 命中 → 继续用旧 secret (降级)"""
    mgr = _FakeSecretManager()
    await mgr.put("web/jwt", "secret-v1")
    iss = JWTIssuer(JWTConfig(secret_ref="secret://web/jwt", secret_manager=mgr))
    # 先初始化 cache
    sec1 = await iss.resolve_secret()
    assert sec1 == b"secret-v1"
    assert mgr.get_count == 1
    # 注入故障
    mgr.fail_get = True
    # 再调: cache 命中, 降级, warning log, 但不抛
    sec2 = await iss.resolve_secret()
    assert sec2 == b"secret-v1"  # 用 cache
    # get_count = 1 (首次成功) + 1 (失败但降级) = 2
    assert mgr.get_count == 2


# ---------------------------------------------------------------------------
# D4-4: SecretManager.get 失败时降级 — cache miss → JWTError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_secret_get_failure_without_cache_raises() -> None:
    """SecretManager.get 失败时, cache miss → 抛 JWTError"""
    mgr = _FakeSecretManager()
    await mgr.put("web/jwt", "secret-v1")
    iss = JWTIssuer(JWTConfig(secret_ref="secret://web/jwt", secret_manager=mgr))
    # 故意不调 resolve_secret 初始化 cache
    # 直接调: cache miss, 调 get 失败
    mgr.fail_get = True
    with pytest.raises(JWTError, match="SecretManager.get"):
        await iss.resolve_secret()


# ---------------------------------------------------------------------------
# D4-5: invalidate_cache 强制下次重读
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalidate_cache_forces_reread() -> None:
    """invalidate_cache → 下次 resolve_secret 必重读"""
    mgr = _FakeSecretManager()
    await mgr.put("web/jwt", "secret-v1")
    iss = JWTIssuer(JWTConfig(secret_ref="secret://web/jwt", secret_manager=mgr))
    await iss.resolve_secret()
    assert mgr.get_count == 1
    # 不 rotate, 直接 invalidate → 强制重读
    iss.invalidate_cache()
    await iss.resolve_secret()
    assert mgr.get_count == 2


# ---------------------------------------------------------------------------
# D4-6: 完整轮换流程 — 旧 token 在 cache TTL 内仍 verify, 新 token 用新 secret
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rotation_old_token_within_cache_ttl() -> None:
    """
    完整轮换: v1 签发 token → rotate → 旧 token 在 cache TTL 内仍 verify

    场景:
      1. v1 签发 token_t1
      2. 调 mgr.rotate → version+1, secret=v2
      3. iss.decode(token_t1) — cache 还是 v1 → verify OK
      4. resolve_secret() → 触发 cache 更新到 v2
      5. iss.decode(token_t1) — cache 已 v2 → verify 失败
    """
    mgr = _FakeSecretManager()
    await mgr.put("web/jwt", "v1-secret")
    iss = JWTIssuer(JWTConfig(secret_ref="secret://web/jwt", secret_manager=mgr))
    # 初始化 cache
    await iss.resolve_secret()
    # 1) v1 签发
    token_v1 = iss.encode("alice")
    assert iss.decode(token_v1)["sub"] == "alice"
    # 2) rotate
    await mgr.rotate("web/jwt", "v2-secret")
    # 3) cache 还是 v1 → 旧 token 仍 verify
    assert iss.decode(token_v1)["sub"] == "alice"
    # 4) 触发 cache 更新
    await iss.resolve_secret()
    # 5) cache 已 v2 → 旧 token 失效
    with pytest.raises(JWTError, match="signature"):
        iss.decode(token_v1)
    # 6) 新 token 用 v2 → verify OK
    token_v2 = iss.encode("bob")
    assert iss.decode(token_v2)["sub"] == "bob"


# ---------------------------------------------------------------------------
# D4-7: 轮换 + G-026 风格的 multi-phase 端到端
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rotation_full_lifecycle_with_invalidate() -> None:
    """
    W36a G-026 Phase A-D 完整端到端:
      Phase A: v1 签发 → verify OK
      Phase B: rotate 到 v2
      Phase C: invalidate_cache → 旧 token 立即失效 (TTL=0 等价)
      Phase D: v2 签发 → verify OK
    """
    mgr = _FakeSecretManager()
    await mgr.put("web/jwt", "v1")
    iss = JWTIssuer(JWTConfig(secret_ref="secret://web/jwt", secret_manager=mgr))
    await iss.resolve_secret()
    # Phase A
    token_a = iss.encode("alice")
    assert iss.decode(token_a)["sub"] == "alice"
    # Phase B
    await mgr.rotate("web/jwt", "v2")
    # Phase C: invalidate 后, 旧 token 立即失效
    iss.invalidate_cache()
    await iss.resolve_secret()  # 拉到 v2
    with pytest.raises(JWTError, match="signature"):
        iss.decode(token_a)
    # Phase D
    token_d = iss.encode("bob")
    assert iss.decode(token_d)["sub"] == "bob"
