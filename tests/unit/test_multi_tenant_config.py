"""
@module tests.unit.test_multi_tenant_config
@brief  W16-② SecurityContext.mode (single/multi) 校验测试

覆盖:
  - 默认 mode=SINGLE 不强制 tenant_id
  - mode=MULTI 时 tenant_id 必填
  - mode=MULTI 时 tenant_id 不能是 'local' (reserved)
  - mode=MULTI 时空白 tenant_id 报错
"""

from __future__ import annotations

import pytest

from agent_swarm.security.context import SecurityContext, TenantMode


def test_default_mode_is_single() -> None:
    ctx = SecurityContext(tenant_id="local", session_id="s1")
    assert ctx.mode == TenantMode.SINGLE


def test_single_mode_allows_local_tenant() -> None:
    """single 模式下 tenant_id="local" 是合法的（向后兼容 W1-W4）"""
    ctx = SecurityContext(tenant_id="local", session_id="s1", mode=TenantMode.SINGLE)
    assert ctx.tenant_id == "local"


def test_single_mode_allows_any_tenant() -> None:
    ctx = SecurityContext(tenant_id="acme-corp", session_id="s1", mode=TenantMode.SINGLE)
    assert ctx.tenant_id == "acme-corp"


def test_multi_mode_requires_tenant_id() -> None:
    """multi 模式下 tenant_id 不能为空"""
    with pytest.raises(ValueError, match="non-empty tenant_id"):
        SecurityContext(tenant_id="", session_id="s1", mode=TenantMode.MULTI)


def test_multi_mode_rejects_whitespace() -> None:
    with pytest.raises(ValueError, match="non-empty tenant_id"):
        SecurityContext(tenant_id="   ", session_id="s1", mode=TenantMode.MULTI)


def test_multi_mode_rejects_reserved_local() -> None:
    """multi 模式下 'local' 是 reserved——避免与单租户默认值混用"""
    with pytest.raises(ValueError, match="reserved tenant_id='local'"):
        SecurityContext(tenant_id="local", session_id="s1", mode=TenantMode.MULTI)


def test_multi_mode_accepts_real_tenant() -> None:
    ctx = SecurityContext(
        tenant_id="acme-corp",
        session_id="s1",
        mode=TenantMode.MULTI,
    )
    assert ctx.tenant_id == "acme-corp"
    assert ctx.mode == TenantMode.MULTI


def test_multi_mode_accepts_uuid_tenant() -> None:
    ctx = SecurityContext(
        tenant_id="550e8400-e29b-41d4-a716-446655440000",
        session_id="s1",
        mode=TenantMode.MULTI,
    )
    assert "550e8400" in ctx.tenant_id


def test_context_asyncio_context_returns_context() -> None:
    """asyncio_context() 返回 contextvars.Context 副本"""
    ctx = SecurityContext(tenant_id="t1", session_id="s1", mode=TenantMode.MULTI)
    c = ctx.asyncio_context()
    import contextvars

    assert isinstance(c, contextvars.Context)


def test_context_immutable() -> None:
    """SecurityContext 是 frozen dataclass"""
    ctx = SecurityContext(tenant_id="t1", session_id="s1", mode=TenantMode.MULTI)
    with pytest.raises(Exception):  # noqa: BLE001, B017  # FrozenInstanceError
        ctx.tenant_id = "t2"  # type: ignore[misc]


def test_tenant_mode_string_values() -> None:
    """TenantMode 是 StrEnum——可与字符串比较"""
    assert TenantMode.SINGLE == "single"
    assert TenantMode.MULTI == "multi"
    # StrEnum 的 str() 返回自身字符串值（不是 "TenantMode.SINGLE"）
    assert str(TenantMode.SINGLE) == "single"
    assert str(TenantMode.MULTI) == "multi"
