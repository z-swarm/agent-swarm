"""
@module agent_swarm.security.tenant_quota
@brief  W16-③ TenantQuota——多租户配额管理（DESIGN §8.4）

P3-PLAN-v2 W16 DoD ③：
  - max_agents / max_concurrent_tasks / max_tokens_per_hour
  - 超限抛 TenantQuotaExceeded + emit quota.exceeded 事件

@note W16 范围：配额定义 + 检查 + 异常；配额的实际执行（拒绝创建 agent / 暂停任务）
      留给 W18（Redis 后端），本 W 范围是"配额存在并能查"。

@note 与 §8.4 关系：
  - §8.4 SecurityContext 已含 tenant_id（W5 起）
  - §8.4 "TenantQuota 落地"——本 W 落地
  - §16.3 #9 / #10 等不直接相关
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


class TenantQuotaExceeded(RuntimeError):
    """Tenant 配额超限——DESIGN §8.4 / P3-PLAN-v2 W16 ③"""

    def __init__(
        self,
        tenant_id: str,
        quota_type: str,  # "agents" | "concurrent_tasks" | "tokens_per_hour"
        limit: int | float,
        current: int | float,
    ) -> None:
        self.tenant_id = tenant_id
        self.quota_type = quota_type
        self.limit = limit
        self.current = current
        super().__init__(
            f"tenant {tenant_id!r} quota exceeded: "
            f"{quota_type} current={current} > limit={limit}"
        )


@dataclass
class TenantQuota:
    """
    单个 tenant 的配额

    @param max_agents            同时存在的最大 agent 数
    @param max_concurrent_tasks  同时 in_progress 的最大 task 数
    @param max_tokens_per_hour   每小时最大 LLM token 消耗
    @param tokens_used_window    token 滑窗起点（unix 时间）
    @param tokens_used           当前窗口内已用 token 数
    """

    max_agents: int = 10
    max_concurrent_tasks: int = 20
    max_tokens_per_hour: int = 1_000_000
    # 内部状态：1 小时滑窗
    tokens_used: int = 0
    tokens_used_window: float = field(default_factory=time.time)

    # 阈值 (DESIGN §16.3 调参空间)
    WINDOW_SECONDS: int = 3600

    def check_agents(self, tenant_id: str, current_count: int) -> None:
        """检查 agent 数；超限抛 TenantQuotaExceeded"""
        if current_count >= self.max_agents:
            raise TenantQuotaExceeded(
                tenant_id=tenant_id, quota_type="agents",
                limit=self.max_agents, current=current_count,
            )

    def check_concurrent_tasks(self, tenant_id: str, current_count: int) -> None:
        if current_count >= self.max_concurrent_tasks:
            raise TenantQuotaExceeded(
                tenant_id=tenant_id, quota_type="concurrent_tasks",
                limit=self.max_concurrent_tasks, current=current_count,
            )

    def check_tokens(self, tenant_id: str, additional_tokens: int) -> int:
        """
        检查 token 配额 + 滑窗重置；返回新累计 token 数

        @note 调用方拿到返回值后应该用它更新 tokens_used
        @note additional_tokens <= 0 跳过累积（用于测试 + 0 token 边界）
        """
        if additional_tokens <= 0:
            return self.tokens_used
        now = time.time()
        # 滑窗过期 → 重置
        if now - self.tokens_used_window > self.WINDOW_SECONDS:
            self.tokens_used = 0
            self.tokens_used_window = now
        if self.tokens_used + additional_tokens > self.max_tokens_per_hour:
            raise TenantQuotaExceeded(
                tenant_id=tenant_id, quota_type="tokens_per_hour",
                limit=self.max_tokens_per_hour,
                current=self.tokens_used + additional_tokens,
            )
        self.tokens_used += additional_tokens
        return self.tokens_used

    def reset_tokens(self) -> None:
        """手动重置 token 计数器（测试 / 周期回滚用）"""
        self.tokens_used = 0
        self.tokens_used_window = time.time()

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_agents": self.max_agents,
            "max_concurrent_tasks": self.max_concurrent_tasks,
            "max_tokens_per_hour": self.max_tokens_per_hour,
            "tokens_used": self.tokens_used,
            "tokens_used_window": self.tokens_used_window,
        }


class TenantQuotaRegistry:
    """
    多 tenant 配额注册表——DESIGN §8.4

    @note W16 范围：内存版（每 tenant 一个 TenantQuota）
    @note W18 范围：可选 Redis 后端（多进程共享）
    """

    def __init__(self) -> None:
        self._quotas: dict[str, TenantQuota] = {}

    def set_quota(self, tenant_id: str, quota: TenantQuota) -> None:
        """为 tenant 设置配额（覆盖已存在）"""
        self._quotas[tenant_id] = quota

    def get_quota(self, tenant_id: str) -> TenantQuota:
        """取 tenant 配额；不存在返默认"""
        if tenant_id not in self._quotas:
            # 兜底默认（单租户场景）
            self._quotas[tenant_id] = TenantQuota()
        return self._quotas[tenant_id]

    def has_quota(self, tenant_id: str) -> bool:
        return tenant_id in self._quotas

    def remove_quota(self, tenant_id: str) -> None:
        self._quotas.pop(tenant_id, None)

    def list_tenants(self) -> list[str]:
        return list(self._quotas.keys())

    def reset_all(self) -> None:
        self._quotas.clear()


__all__ = [
    "TenantQuota",
    "TenantQuotaExceeded",
    "TenantQuotaRegistry",
]
