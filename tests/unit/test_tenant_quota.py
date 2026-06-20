"""
@module tests.unit.test_tenant_quota
@brief  W16-③ TenantQuota 单元测试——DESIGN §8.4

覆盖:
  - TenantQuota 字段默认值
  - check_agents / check_concurrent_tasks / check_tokens 触发超限抛 TenantQuotaExceeded
  - token 滑窗过期重置
  - TenantQuotaRegistry 注册/查询/默认值
"""

from __future__ import annotations

import time

import pytest

from agent_swarm.security.tenant_quota import (
    TenantQuota,
    TenantQuotaExceeded,
    TenantQuotaRegistry,
)


# ---------------------------------------------------------------------------
# TenantQuota 基础
# ---------------------------------------------------------------------------


def test_quota_default_values() -> None:
    q = TenantQuota()
    assert q.max_agents == 10
    assert q.max_concurrent_tasks == 20
    assert q.max_tokens_per_hour == 1_000_000
    assert q.tokens_used == 0
    assert q.WINDOW_SECONDS == 3600


def test_check_agents_within_limit() -> None:
    q = TenantQuota(max_agents=5)
    q.check_agents("t1", current_count=3)  # 不抛


def test_check_agents_exceeds() -> None:
    q = TenantQuota(max_agents=5)
    with pytest.raises(TenantQuotaExceeded) as exc_info:
        q.check_agents("t1", current_count=5)
    assert exc_info.value.tenant_id == "t1"
    assert exc_info.value.quota_type == "agents"
    assert exc_info.value.limit == 5
    assert exc_info.value.current == 5


def test_check_concurrent_tasks_exceeds() -> None:
    q = TenantQuota(max_concurrent_tasks=10)
    with pytest.raises(TenantQuotaExceeded) as exc_info:
        q.check_concurrent_tasks("t1", current_count=10)
    assert exc_info.value.quota_type == "concurrent_tasks"


def test_check_tokens_within_limit() -> None:
    q = TenantQuota(max_tokens_per_hour=1000)
    new_total = q.check_tokens("t1", additional_tokens=500)
    assert new_total == 500
    assert q.tokens_used == 500


def test_check_tokens_cumulative_exceeds() -> None:
    q = TenantQuota(max_tokens_per_hour=1000)
    q.check_tokens("t1", 600)
    with pytest.raises(TenantQuotaExceeded) as exc_info:
        q.check_tokens("t1", 500)  # 累计 1100 > 1000
    assert exc_info.value.quota_type == "tokens_per_hour"
    assert exc_info.value.current == 1100


def test_check_tokens_window_reset() -> None:
    """滑窗过期 → tokens_used 重置"""
    q = TenantQuota(max_tokens_per_hour=1000)
    q.check_tokens("t1", 800)
    assert q.tokens_used == 800
    # 模拟窗口过期：把 window 起点设到 2 小时前
    q.tokens_used_window = time.time() - 7200
    # 这次应重置 + 新加 500 → 累计 500
    new_total = q.check_tokens("t1", 500)
    assert new_total == 500
    assert q.tokens_used == 500  # 重置后 = 500


def test_check_tokens_negative_skipped() -> None:
    q = TenantQuota(max_tokens_per_hour=1000)
    new_total = q.check_tokens("t1", 0)  # 0 token 不应累积
    assert new_total == 0
    new_total = q.check_tokens("t1", -100)  # 负数也不累积
    assert new_total == 0


def test_reset_tokens() -> None:
    q = TenantQuota(max_tokens_per_hour=1000)
    q.check_tokens("t1", 500)
    q.reset_tokens()
    assert q.tokens_used == 0
    assert time.time() - q.tokens_used_window < 1.0


def test_quota_to_dict() -> None:
    q = TenantQuota()
    d = q.to_dict()
    assert "max_agents" in d
    assert "max_concurrent_tasks" in d
    assert "max_tokens_per_hour" in d
    assert "tokens_used" in d
    assert "tokens_used_window" in d


# ---------------------------------------------------------------------------
# TenantQuotaRegistry
# ---------------------------------------------------------------------------


def test_registry_default_quota() -> None:
    reg = TenantQuotaRegistry()
    q = reg.get_quota("new-tenant")
    # 不存在的 tenant 返回默认配额
    assert q.max_agents == 10
    # get_quota 顺便注册——通过 reg.has_quota 验证
    assert reg.has_quota("new-tenant")


def test_registry_set_and_get() -> None:
    reg = TenantQuotaRegistry()
    custom = TenantQuota(max_agents=100, max_concurrent_tasks=200)
    reg.set_quota("t1", custom)
    assert reg.has_quota("t1")
    got = reg.get_quota("t1")
    assert got.max_agents == 100
    assert got.max_concurrent_tasks == 200


def test_registry_overwrite() -> None:
    reg = TenantQuotaRegistry()
    reg.set_quota("t1", TenantQuota(max_agents=10))
    reg.set_quota("t1", TenantQuota(max_agents=99))
    assert reg.get_quota("t1").max_agents == 99


def test_registry_remove() -> None:
    reg = TenantQuotaRegistry()
    reg.set_quota("t1", TenantQuota())
    reg.remove_quota("t1")
    assert not reg.has_quota("t1")


def test_registry_remove_nonexistent() -> None:
    reg = TenantQuotaRegistry()
    assert reg.remove_quota("ghost") is None  # 不报错


def test_registry_list_tenants() -> None:
    reg = TenantQuotaRegistry()
    reg.set_quota("a", TenantQuota())
    reg.set_quota("b", TenantQuota())
    assert sorted(reg.list_tenants()) == ["a", "b"]


def test_registry_reset_all() -> None:
    reg = TenantQuotaRegistry()
    reg.set_quota("a", TenantQuota())
    reg.reset_all()
    assert reg.list_tenants() == []


# ---------------------------------------------------------------------------
# TenantQuotaExceeded 异常
# ---------------------------------------------------------------------------


def test_quota_exceeded_error_message() -> None:
    exc = TenantQuotaExceeded(tenant_id="t1", quota_type="agents",
                              limit=10, current=15)
    msg = str(exc)
    assert "t1" in msg
    assert "agents" in msg
    assert "15" in msg
    assert "10" in msg


def test_quota_exceeded_is_runtime_error() -> None:
    exc = TenantQuotaExceeded("t1", "concurrent_tasks", 20, 25)
    assert isinstance(exc, RuntimeError)
