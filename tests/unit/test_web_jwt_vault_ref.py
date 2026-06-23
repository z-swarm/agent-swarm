"""
@module tests.unit.test_web_jwt_vault_ref
@brief  P5-W36c vault:// URI 单测 (≥6 cases)

覆盖:
  - parse_secret_ref: vault://path (无 field) / vault://path#field (有 field)
  - parse_secret_ref 错误路径: vault:// 空 path / 空 field
  - SecretRef field 字段 (W36c 新增)
  - JWTConfig vault:// 模式
  - JWTIssuer.resolve_secret vault:// 走 SecretManager + field JSON 提取
  - JWTIssuer vault:// 失败: JSON 解析失败 / field 不存在
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
    SecretRef,
    parse_secret_ref,
)

# ---------------------------------------------------------------------------
# Fake VaultSecretManager (JSON 文档存 value)
# ---------------------------------------------------------------------------


class _FakeVaultSecretManager(SecretManager):
    """
    @brief W36c 测试用 VaultSecretManager fake

    特性:
      - in-memory dict 存储
      - value 是 JSON 字符串 (模拟 Vault KV v2 多 field 文档)
      - put 每次 version+1
    """

    def __init__(self) -> None:
        self._store: dict[str, Secret] = {}
        self._version: dict[str, int] = {}

    async def get(self, key: str) -> Secret:
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
# parse_secret_ref — vault:// 协议
# ---------------------------------------------------------------------------


def test_parse_vault_uri_no_field() -> None:
    """vault://path (无 #field) → kind=vault, field=None"""
    ref = parse_secret_ref("vault://web/jwt-secret")
    assert ref.kind == "vault"
    assert ref.value == "web/jwt-secret"
    assert ref.field is None


def test_parse_vault_uri_with_field() -> None:
    """vault://path#field → kind=vault, value=path, field=field"""
    ref = parse_secret_ref("vault://web/jwt-secret#current")
    assert ref.kind == "vault"
    assert ref.value == "web/jwt-secret"
    assert ref.field == "current"


def test_parse_vault_uri_empty_path_raises() -> None:
    """vault:// (空 path) → ValueError"""
    with pytest.raises(ValueError, match="empty Vault path"):
        parse_secret_ref("vault://")


def test_parse_vault_uri_empty_field_raises() -> None:
    """vault://path# (空 field) → ValueError"""
    with pytest.raises(ValueError, match="empty Vault field"):
        parse_secret_ref("vault://web/jwt-secret#")


def test_parse_vault_uri_complex_path() -> None:
    """vault:// 复杂 path (含多级)"""
    ref = parse_secret_ref("vault://team/auth/prod/jwt#v1")
    assert ref.kind == "vault"
    assert ref.value == "team/auth/prod/jwt"
    assert ref.field == "v1"


# ---------------------------------------------------------------------------
# SecretRef field 字段
# ---------------------------------------------------------------------------


def test_secretref_vault_kind_with_field() -> None:
    """SecretRef vault kind 带 field 字段"""
    ref = SecretRef(kind="vault", value="web/jwt", field="key")
    assert ref.kind == "vault"
    assert ref.value == "web/jwt"
    assert ref.field == "key"


def test_secretref_vault_kind_no_field_default() -> None:
    """SecretRef vault kind 缺省 field=None"""
    ref = SecretRef(kind="vault", value="web/jwt")
    assert ref.field is None


def test_secretref_w36a_kinds_still_work() -> None:
    """W36a 老的 3 kinds 仍工作 (field 缺省 None)"""
    r1 = SecretRef(kind="literal", value="x")
    r2 = SecretRef(kind="env", value="VAR")
    r3 = SecretRef(kind="secret_ref", value="key")
    assert r1.field is None
    assert r2.field is None
    assert r3.field is None


# ---------------------------------------------------------------------------
# JWTConfig vault:// 模式
# ---------------------------------------------------------------------------


def test_jwtconfig_vault_mode_works() -> None:
    """JWTConfig vault:// 模式 (W36c)"""
    mgr = object()  # 实际 SecretManager, 此处仅校验 config
    cfg = JWTConfig(secret_ref="vault://web/jwt#key", secret_manager=mgr)  # type: ignore[arg-type]
    assert cfg.secret is None
    assert cfg.secret_ref == "vault://web/jwt#key"
    assert cfg.secret_manager is mgr


# ---------------------------------------------------------------------------
# JWTIssuer.resolve_secret vault:// 模式
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_vault_no_field_uses_value_directly() -> None:
    """vault://path (无 field) → 直接用 value"""
    mgr = _FakeVaultSecretManager()
    await mgr.put("web/jwt", "plain-value-not-json")
    iss = JWTIssuer(JWTConfig(secret_ref="vault://web/jwt", secret_manager=mgr))
    sec = await iss.resolve_secret()
    assert sec == b"plain-value-not-json"


@pytest.mark.asyncio
async def test_resolve_vault_with_field_extracts_json() -> None:
    """vault://path#field → JSON 提取 field"""
    mgr = _FakeVaultSecretManager()
    doc = json.dumps({
        "current": "jwt-secret-v1",
        "previous": "jwt-secret-v0",
        "metadata": {"rotated_at": "2026-06-24"},
    })
    await mgr.put("web/jwt-secret", doc)
    iss = JWTIssuer(
        JWTConfig(secret_ref="vault://web/jwt-secret#current", secret_manager=mgr),
    )
    sec = await iss.resolve_secret()
    assert sec == b"jwt-secret-v1"


@pytest.mark.asyncio
async def test_resolve_vault_field_not_in_doc_raises() -> None:
    """vault://path#missing_field → JWTError"""
    mgr = _FakeVaultSecretManager()
    doc = json.dumps({"current": "v1"})
    await mgr.put("web/jwt-secret", doc)
    iss = JWTIssuer(
        JWTConfig(secret_ref="vault://web/jwt-secret#nonexistent", secret_manager=mgr),
    )
    with pytest.raises(JWTError, match="field not in document"):
        await iss.resolve_secret()


@pytest.mark.asyncio
async def test_resolve_vault_value_not_json_raises() -> None:
    """vault://path#field 但 value 不是 JSON → JWTError"""
    mgr = _FakeVaultSecretManager()
    await mgr.put("web/jwt-secret", "not-a-json-document")
    iss = JWTIssuer(
        JWTConfig(secret_ref="vault://web/jwt-secret#field", secret_manager=mgr),
    )
    with pytest.raises(JWTError, match="not JSON"):
        await iss.resolve_secret()


@pytest.mark.asyncio
async def test_resolve_vault_rotation_invalidates_cache() -> None:
    """vault:// 模式轮换 → cache 失效"""
    mgr = _FakeVaultSecretManager()
    doc_v1 = json.dumps({"current": "secret-v1"})
    await mgr.put("web/jwt", doc_v1)
    iss = JWTIssuer(
        JWTConfig(secret_ref="vault://web/jwt#current", secret_manager=mgr),
    )
    sec1 = await iss.resolve_secret()
    assert sec1 == b"secret-v1"
    # 轮换
    doc_v2 = json.dumps({"current": "secret-v2"})
    await mgr.rotate("web/jwt", doc_v2)
    sec2 = await iss.resolve_secret()
    assert sec2 == b"secret-v2"
    assert iss._cached_version == 2
