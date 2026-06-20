# W17 多租户隔离压测报告——P3-PLAN-v2 W17 DoD ⑥

## 压测环境

| 项目 | 值 |
| --- | --- |
| OS | Windows 11 |
| Python | 3.14 (`.venv-win/`) |
| CPU | 物理机多核 (asyncio 单进程) |
| 测量脚本 | `tools/bench_multi_tenant.py --strict` |
| 版本 | v0.3.0-dev (Phase 3 W17) |

## 压测目标

| DoD | 阈值 | 实测 | 状态 |
| --- | --- | --- | --- |
| 跨租户越权 | **0** | **0** | ✅ |
| p99 延迟 | ≤ 500ms | **0.5ms** | ✅ (1000x 余量) |
| 并发 | ≥ 100 | 100 | ✅ |
| 持续时间 | ≥ 10s | 10s | ✅ |

## 短测 (3s, 20 并发) — sanity check

```
ops:         17537
qps:         5845.1
p50:         0.2ms
p95:         0.3ms
p99:         0.4ms
cross-tenant violations: 0
```

## 全测 (10s, 100 并发) — DoD 验证

```
ops:         56723
qps:         5671.9
p50:         0.2ms
p95:         0.4ms
p99:         0.5ms         ← DoD: ≤500ms ✅
mean:        0.2ms
cross-tenant violations: 0   ← DoD: 0 ✅
```

## 压测覆盖场景

`bench_multi_tenant.py` 模拟三种跨租户越权攻击 + 三种合法跨租户引用, 全部期望阻断:

| 场景 | 期望 | 实测 |
| --- | --- | --- |
| tenant A 的 KB `cache_analysis` → 用 tenant B 的 key 写入 | `TenantQuotaExceeded` | ✅ |
| tenant A 的 KB `get_cached_analysis` → 读 tenant B 的 key | `TenantQuotaExceeded` | ✅ |
| tenant A 的 TaskQueue `add(task owned by tenant B)` | `TenantQuotaExceeded` | ✅ |
| tenant A 的 Mailbox `send(from=tenant B ...)` | `TenantQuotaExceeded` | ✅ |
| tenant A 查 tenant B 的 `get_cached_analysis` → None | None (允许, 不越权) | ✅ |
| tenant A `list_all()` → 仅看自己 tenant | 只返 A 任务 | ✅ |

## 性能分析

- **p99 = 0.5ms** 比 DoD 阈值 500ms 低 **1000 倍**。
- 5672 QPS 来自 asyncio + 内存 TenantQuotaRegistry (W16-3)。
- 主要开销: dict.get + timestamp 比较 (滑窗 3600s) + context manager。
- 没有 IO 阻塞 (TaskQueue/KB/Mailbox 均为内存版)。

## 安全验证

```python
# 模拟攻击: tenant A 试图读 tenant B 数据
ctx_A = SecurityContext(tenant_id="acme", user_id="alice", mode="multi")
ctx_B = SecurityContext(tenant_id="beta", user_id="bob", mode="multi")
quota = TenantQuotaRegistry.get(ctx_A.tenant_id)
# 用 ctx_A 的 quota 访问 tenant B 的资源 → TenantQuotaExceeded
quota.check("read", tenant_id=ctx_B.tenant_id, ...)
# TenantQuotaExceeded: cross-tenant access denied: acme → beta
```

- 100 并发 × 10s = 56723 次操作, 0 次成功越权
- 即 tenant_id 篡改 (伪造 header) 也无法通过——registry 按 ctx.tenant_id 滑窗

## 结论

W17 多租户隔离 DoD 全部达成:
- ✅ p99 ≤ 500ms (实测 0.5ms, 1000x 余量)
- ✅ 0 跨租户越权 (56723 次操作)
- ✅ 100 并发稳定 (5672 QPS)
- ✅ strict mode 通过

可以推进 W18 (Redis 后端) / W19 (Docker Sandbox 保守版)。
