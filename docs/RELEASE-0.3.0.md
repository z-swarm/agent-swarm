# agent-swarm 0.3.0 Release Notes

**Release Date**: 2026-06-21
**Tag**: `0.3.0` (commit `e6d08e6`)
**Status**: Ready for PyPI publish (requires user credentials)

## 0.3.0 是什么

Phase 3 (W14-W21) 8 周交付完成。框架从 Phase 2 的"基础多 agent 协作"演进到
"生产级多租户 + 跨通道 + 可观测 + 可插拔后端"。

## 核心新功能

### W14 — MCP SSE + Doctor (CI 落地)

- `SseMCPClient` — HTTP POST + SSE 流解析 (JSON-RPC 2.0)
- `CircuitBreaker` — CLOSED/OPEN/HALF_OPEN + 指数退避
- `ReconnectingMCPClient` — 单次重连防无限循环
- `agent-swarm doctor` — 4 类健康检查 (LLM/SQLite/MCP/Secrets)
- CI workflow — `security-scan` + `windows-smoke` job

### W15 — PrometheusSink + 5 核心指标

- `framework_tasks_total` / `framework_llm_tokens_total`
- `framework_cas_conflict_total` / `framework_mcp_circuit_state`
- `framework_approval_pending_count`
- `/metrics` + `/healthz` HTTP 端点 (aiohttp)
- Grafana dashboard (4 面板) + 5 个 recipes
- `agent_review.py --require-human-review --approve-override` 守门

### W16-W17 — 多租户隔离 + 跨通道 Session 绑定

- `SecurityContext.tenant_id` + `mode` (SINGLE/MULTI)
- 100 并发跨租户压测: 5672 QPS / 0 越权 / p99=0.5ms
- 跨通道 session 绑定 (飞书 ↔ CLI ↔ WebSocket)

### W18 — TaskQueue 后端抽象

- `MemoryBackend` (默认) / `RedisBackend` (生产)
- CAS (Compare-And-Set) 语义统一
- fakeredis 支持, 无需 Redis server
- 100 agent 并发 claim: winner-takes-all 验证 (G-020)

### W19 — Docker Sandbox (保守版, opt-in)

- 20 个容器逃逸尝试拦截
- CIS Docker Benchmark 10 项
- 性能: 启动 ≤500ms (本地镜像)

### W20 — Vault 密钥管理 + MCP Source 分级

- `hvac` 集成 (可选)
- MCP server source: `official` / `community` / `private` (显式声明)
- BREAKING: 必须显式 `source:` 字段

## 测试 / 守门数据

| 指标 | Phase 2 末 | Phase 3 末 (0.3.0) |
|-----|----------|----------------|
| 测试数 | ~330 | **975** (含 Golden Cases 34) |
| Lint errors (ruff) | ? | **0** |
| mypy errors | ? | **0** |
| Golden Cases | 0 | **3** (G-018/019/020) |
| 文档 | 6 篇 | **14** 篇 |
| Bench tools | 0 | **3** (multi_tenant / storage / sandbox) |
| Examples | 6 | **9** |

## BREAKING CHANGES (迁移注意)

```yaml
# v0.2.0 → v0.3.0 必填
mcp_servers:
  my_server:
    transport: stdio
    command: [...]
    source: community  # NEW: official / community / private
```

## 已知限制

- 137 个测试在 Windows 跳过 (P3-WIN marker), Linux CI 全跑
  - subprocess / shell 语义差异
  - 硬编码 `/tmp` 路径
  - CI: ubuntu-latest 跑全量, windows-latest 跑 smoke

## 发布清单

- [x] git tag `0.3.0` 创建 (本地)
- [x] sdist + wheel 构建 (PASSED twine check)
- [x] ruff 0 errors / mypy 0 errors
- [x] 975 tests passed / 138 skipped / 0 failed
- [x] 3 个 Golden Cases 通过
- [ ] **git push origin main** (需要 SSH/GitHub credentials)
- [ ] **TestPyPI 0.3.0a1** (需要 TestPyPI token)
- [ ] **PyPI 0.3.0 stable** (需要 PyPI token)

## 发布命令 (待用户执行)

```bash
# 1. 推 commits + tag
git push origin main
git push origin 0.3.0

# 2. 上传 TestPyPI
twine upload --repository testpypi dist/agent_swarm-0.3.0*

# 3. 验证 TestPyPI 安装
pip install -i https://test.pypi.org/simple/ agent-swarm==0.3.0

# 4. 上传正式 PyPI
twine upload dist/agent_swarm-0.3.0*

# 5. 验证正式安装
pip install agent-swarm==0.3.0
```

## 引用

- CHANGELOG.md — 完整变更日志
- docs/PHASE3-RETRO.md — Phase 3 复盘
- docs/PHASE3-PLAN-2026-06-20.md — Phase 3 计划
- docs/MULTI-TENANT-REPORT.md — 多租户压测报告
- docs/STORAGE-BENCH.md — 后端压测报告
- docs/SANDBOX-BENCH.md — Sandbox 性能基准
- docs/RISK-LOG.md — 20 风险登记
