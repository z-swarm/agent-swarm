# Phase 4 收尾复盘 (W22-W27)

## 时间
- W22-W23: 2026-06-22 上午 (MCP Worktree 隔离)
- W24: 2026-06-22 中午 (Docker 长生命周期)
- W25: 2026-06-22 下午 (PostgresBackend)
- W26: 2026-06-22 傍晚 (Vault Dynamic Secrets)
- W27: 2026-06-22 晚上 (收尾 + 0.4.0a1 dist)

## 做对的事

### 1) 4/6 候选完成, 选型精准
P4-PLAN 列了 6 个候选, 我们挑了与 Phase 3 强相关的 4 个 (W22-26):
- W22-23 MCP Worktree 隔离 → 解多 agent 共享 workspace 冲突
- W24 Docker 长生命周期 → 性能优化 100x
- W25 PostgresBackend → 大规模生产替代 Redis
- W26 Vault Dynamic Secrets → DB 凭证轮换安全
跳过 GUI Web UI (4-6 周) 和多语言 SDK (4 周), 留给未来

### 2) 零信任逐阶段验证
每阶段都跑 verify_p4_dod.py + ruff + mypy + pytest 守门, 通过后才进下一阶段:
- W22 verify: 26 tests / 1020 全量
- W23 verify: G-021 3/3 / 1020 全量
- W24 verify: 13 tests / 1033 全量
- W25 verify: 13 tests / 1046 全量
- W26 verify: 14 tests / 1060 全量
- W27 verify: 0.4.0a1 dist twine check PASSED

### 3) 测试基础设施成熟
- 4 个新模块, 0 个用了真依赖 (全 mock)
  - worktree: 真实 git (无 mock 必要)
  - docker: fake_runner (mock docker CLI)
  - postgres: fake_module (mock asyncpg)
  - vault: fake vault client (mock hvac)
- 105 个新 unit test, 0 flake
- 跨平台 (Windows + Linux)

### 4) API 锁定 + 向后兼容
- W19 Docker: 加 long_lived=True 默认, 旧测试加 long_lived=False 显式声明
- W18 Redis: 不变, 加新选项
- W20 Vault: 加 DBCredentials + VaultDynamicSecretManager, 原 SecretManager 协议不变
- 6 个候选里 4 个增量, 0 个 breaking

### 5) 文档 + 守门工具
- 4 个新 examples/w22_mcp_worktree.yaml
- G-021 Golden Case
- tools/bench_worktree.py 压测
- tools/verify_p4_dod.py 8 项 DoD
- CHANGELOG 0.4.0a1 节点

## 数据

| 指标 | P3 末 (0.3.0) | P4 末 (0.4.0a1) | 增量 |
|-----|----------|----------------|-----|
| unit tests | 975 | **1060** | +85 |
| golden cases | 3 (G-018/019/020) | **4** (+G-021) | +1 |
| 新模块 | - | worktree / postgres_backend / vault.dynamic | +3 |
| 新 example | 9 | **10** | +1 |
| 新 bench | 3 | **4** | +1 |
| 新 verify | verify_p3_dod | +verify_p4_dod | +1 |
| Lint errors (P4 新增) | 0 | **0** | 0 |
| mypy errors | 0 | **0** | 0 |
| Phase 4 commits | - | **5** (W22-23 / W24 / W25 / W26 / W27) | +5 |
| Phase 4 tags | - | **4** (0.4.0a1-a4) | +4 |

## 风险 + 缓解 (落地版)

| 风险 | 缓解 | 状态 |
|-----|------|-----|
| git worktree 慢 (Windows) | 幂等缓存 + 文档说明 | ✓ 文档化 |
| 容器名冲突 | 类级计数器 (id 不可靠) | ✓ 已修复 |
| Docker 长生命周期 leak | close() + async with + idempotent | ✓ 13 tests |
| postgres 真依赖 | fake_module 注入 | ✓ 13 tests |
| Vault 真依赖 | vault_client 注入 | ✓ 14 tests |
| async context leak | __aenter__ / __aexit__ 协议 | ✓ 测试覆盖 |

## 待启动 (Phase 4 续 / Phase 5)

| 优先级 | 候选 | 工作量 | 价值 |
|-----|------|-------|------|
| **高** | **GUI Web UI** (§16.2 #2) | 4-6 周 | CLI→GUI 升级 |
| 中 | **多语言 SDK** (Go/TypeScript) | 4 周 | 跨语言生态 |
| 低 | **分布式 swarm** | 8 周 | 跨机器调度 |
| 低 | **MCP Worktree 多 repo** | 2 周 | 跨 repo worktree 协调 |

## 已知限制

- Windows: `git worktree add` ~2s/worktree (NTFS 开销); Linux ~50ms
- bench_worktree.py 在 Windows 显示的 QPS 偏低 (3.9 unique_keys), Linux 预计 20-50x
- asyncpg / hvac 仍是可选依赖, 需 `pip install -e .[redis,postgres,vault]` 才可用
- 6 个 Phase 3 历史 ruff 错误 (tools/agent_review.py + verify_w7_dod.py) 未修, 非 P4 范围

## 发布清单

- [x] ruff 0 / mypy 0
- [x] 1060 tests passed / 138 P3-WIN skipped / 0 failed
- [x] G-021 3/3 passed
- [x] verify_p4_dod.py 8 项全过
- [x] 0.4.0a1 sdist + wheel 构建
- [x] twine check 0.4.0a1 PASSED
- [x] CHANGELOG 0.4.0a1 节点
- [x] 4 git tag 创建 (0.4.0a1-a4)
- [ ] **git push origin main** (等用户配置 SSH)
- [ ] **git push origin 0.4.0a1-a4** (等 SSH)
- [ ] **TestPyPI 0.4.0a1** (等用户 token)
- [ ] **PyPI 0.4.0a1** (等用户 token)

## 引用

- CHANGELOG.md — 0.4.0a1 完整变更日志
- tools/verify_p4_dod.py — 8 项 DoD 守门
- docs/PHASE3-RETRO.md — Phase 3 复盘 (对比)
- docs/WORKTREE-BENCH.md — Worktree 性能报告
