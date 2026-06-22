# Changelog

All notable changes to agent-swarm will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.4.0a1] - 2026-06-22

### Phase 4 启动 (W22-W23 MCP Worktree 隔离)

#### W22: WorktreeManager 核心 (manager.py)
- **新增**: `WorktreeManager(repo_root, base_dir)` ——per-agent git worktree 隔离
- **新增**: `WorktreeHandle` dataclass (path / branch / agent_id / tenant_id / session_id / created_at / key)
- **API**: `acquire(tenant_id, session_id, agent_id) → WorktreeHandle` (幂等)
- **API**: `release(handle, force=False)` / `list_active()` / `get(key)` / `cleanup_orphans(ttl)` / `cleanup_all()`
- **异常**: `WorktreeError` / `WorktreeRepoError` / `WorktreeConflictError`
- **并发安全**: per-tenant `threading.Lock` + 同 (tenant, session, agent) 幂等
- **测试**: `tests/unit/test_worktree_manager.py` 26 cases (基础 / 并发 / 隔离 / cleanup / 路径安全)
- **路径处理**: `_sanitize` 把任意字符串转安全标识符; `_is_git_repo` 跨平台 (Windows 正反斜杠归一)

#### W23: MCP 集成 (integration.py)
- **新增**: `${WORKTREE_PATH}` 占位符 → 注入 `MCPServerConfig.command` / `cwd` / `env`
- **新增**: `substitute_placeholders(config, worktree_path)` 函数
- **新增**: `validate_config(config)` 拒绝 token/url 含占位符
- **新增**: `find_placeholders(config)` 定位占位符
- **新增**: `WorktreeIntegration(manager)` 高层封装: `acquire_for_agent` / `release_for_agent` / `materialize_config`
- **新增**: `examples/w22_mcp_worktree.yaml` ——2 worker 共享 repo, 各自 worktree
- **新增**: G-021 Golden Case `tests/golden/test_g021_worktree_isolation.py` 3 cases (3 agent 100 文件 / 10 并发 / 跨租户)
- **新增**: `tools/bench_worktree.py` 压测: 50 unique_keys (QPS=3.9) + same_key (QPS=45.3)
- **新增**: `tools/verify_p4_dod.py` 8 项 DoD 守门

#### W23 测试
- unit: 42 passed (manager 26 + integration 16)
- golden: G-021 3/3 passed
- 全量: 1020 passed / 138 skipped (P3-WIN) / 0 failed
- ruff: 0 errors (P4 新增模块) / mypy: 0 errors

#### 已知限制
- Windows 下 `git worktree add` ~2s/worktree (NTFS 开销); Linux ~50ms
- `tools/agent_review.py` / `tools/verify_w7_dod.py` 仍有 6 个 P3 历史 ruff 错误 (非 P4 范围)

## [0.3.0] - 2026-08-29

### Phase 3 收尾 (W14-W21)

#### W14a — MCP SSE 传输 + 重连/熔断 (5ea044d)
- **新增**: `SseMCPClient` (HTTP POST + SSE 流解析) — JSON-RPC 2.0 over HTTP+SSE
- **新增**: `CircuitBreaker` 三态 (CLOSED/OPEN/HALF_OPEN) + 指数退避 0.5/1/2/4/8 秒
- **新增**: `ReconnectingMCPClient` 单次重连防无限循环 + 43 tests
- **新增**: G-018 Golden Case — MCP server crash 9 connect attempts 3 reconnects
- **新增**: `tools/count_reconnect.py` 验证脚本 (12.2s 实测)

#### W14b — CI 落地 + Doctor (574f81c)
- **新增**: `agent-swarm doctor` 子命令 — LLM/SQLite/MCP/Secrets 4 类检查, exit 0/1/2
- **新增**: `docs/concepts.md` (Agent/Task/Mailbox/KB/AdversarialVerifier 5 章节)
- **新增**: `docs/troubleshooting.md` (10 章节 ≥10 错误)
- **CI**: `.github/workflows/ci.yml` 新增 security-scan + windows-smoke job
- **测试**: 21 doctor tests

#### W15 — PrometheusSink + 5 核心指标 (47d3f9d)
- **新增**: `PrometheusSink` + aiohttp `/metrics` + `/healthz` 端点
- **指标**: `framework_tasks_total` / `framework_llm_tokens_total` / `framework_cas_conflict_total` / `framework_mcp_circuit_state` / `framework_approval_pending_count`
- **新增**: `tools/agent_review.py --require-human-review --approve-override --fail-on` 守门 (CRITICAL exit 2)
- **新增**: `docs/grafana/dashboard.json` 4 面板 + `docs/recipes/README.md` 5 recipes
- **新增**: `docs/RISK-LOG.md` 20 风险登记
- **新增**: G-019 Golden Case — agent_review 识别真问题
- **依赖**: `prometheus-client>=0.20.0`
- **测试**: 25 prometheus_sink + 7 G-019

#### W16 — 多租户隔离 (593e35d)
- **新增**: `SecurityContext.mode` (TenantMode) + `__post_init__` 校验 (multi 拒绝 empty/whitespace/local reserved)
- **新增**: `TenantQuota` + `TenantQuotaRegistry` 滑窗 (3600s) 跨租户拦截
- **新增**: `patched_create_task` 自动注入 SecurityContext (via `ctx.asyncio_context()`)
- **新增**: `tools/audit_create_task.py` 扫 8 个裸 `asyncio.create_task` (与 lint 一致)
- **新增**: `tools/check_sql_lint.py` SQL 注入审计 (启发式 + 邻近窗 + SQLITE_MASTER 例外)
- **新增**: `tools/bench_multi_tenant.py` 100 并发跨租户压测 (5672 QPS / p99=0.5ms / 0 越权)
- **测试**: 17 TenantQuota + 11 MultiTenantConfig + 14 cross-tenant
- **示例**: `examples/w16_multi_tenant.yaml`

#### W17 — 跨通道 Session 合并 (593e35d / 269563a)
- **新增**: `SessionBindingManager` (内存版 + 可选 SQLite) — `(tenant_id, identity_key) → session_id`
- **新增**: `ChannelIdentity` 注册 + `resolve_user` 跨通道身份合并
- **新增**: `bind_or_get_session` 跨通道共享 (飞书 + CLI 同 user_id → 同一 session)
- **新增**: `tools/no_bare_create_task.py` lint 守门 (只对 `src/agent_swarm/` 生效, 允许 `context=` 显式参数)
- **新增**: `docs/MULTI-TENANT-REPORT.md` 100 并发实测 (56723 ops / 10s / 0 越权 / p99=0.5ms)
- **测试**: 17 SessionBinding (含跨通道合并 + tenant 隔离 + SQLite 持久化)
- **示例**: `examples/w17_cross_channel.yaml`

#### W18 — Redis 后端 (c1a5d73 / c8f4540)
- **新增**: `TaskQueueBackend` ABC + `StoredTask` 序列化
- **新增**: `MemoryBackend` — 单进程 asyncio.Lock CAS
- **新增**: `RedisBackend` — WATCH/MULTI/EXEC 乐观锁 (W18-4 多进程并发安全)
- **新增**: G-020 Golden Case — 100 agent 并发 claim → 1 OK + 99 conflict (version=1, assigned 持久化)
- **新增**: `tools/bench_storage.py` — Memory 188k QPS / Redis (fakeredis) 3478 QPS
- **新增**: `docs/STORAGE-BENCH.md` 自动生成
- **依赖**: `[redis] extras` — `redis>=5.0.0` + `fakeredis>=2.20.0`
- **测试**: 17 backend + 2 G-020
- **示例**: `examples/w18_redis_backend.yaml`

#### W19 — Docker Sandbox 保守版 (W19 commit)
- **新增**: `SandboxMode.DOCKER` 枚举值 (默认仍 WORKSPACE_ONLY — 向后兼容)
- **新增**: `DockerSandboxManager` + `DockerConfig` + `EscapeAttempt` + `CISDockerCheck`
- **新增**: 10 条 CIS Docker Benchmark 关键项 (4.1/5.2-7/5.12-14)
- **新增**: 20 条容器逃逸拦截 (`CONTAINER_ESCAPE_ATTEMPTS`) — mount/privileged/cap-add/host net/nsenter/chroot/cgroup/ctr
- **新增**: `agent-swarm doctor` 新增 `sandbox.docker` 检查
- **新增**: `tools/bench_sandbox.py` — Workspace 1308 QPS / Docker 163 QPS (mock 50ms)
- **新增**: `docs/SANDBOX-BENCH.md`
- **测试**: 20 docker_sandbox + 24 container_escape (含合法命令边界)
- **示例**: `examples/w19_docker_sandbox.yaml`

#### W20 — Vault 密钥 + MCP source 分级 (W20 commit)
- **新增**: `SecretManager` ABC + `EnvSecretManager` (read-only) + `VaultSecretManager`
- **新增**: AppRole 认证 + KV v2 secret engine + 内存缓存 TTL (默认 5 分钟)
- **新增**: `rotation_due` 预警 (提前 7 天) — 通过 ObservabilityBus emit
- **新增**: `MCPSource` + `SOURCE_TO_DEFAULT_RISK` + `validate_source` (强制必填)
- **新增**: `MCPServerConfig.source` 字段 (`official|community|private`) — YAML 缺此字段启动失败 (§16.3 #10 收紧)
- **默认分级**: official → LOW / private → MEDIUM / community → HIGH (生产默认禁 + Approval 强制)
- **新增**: `bump_tool_risk_by_source` (取 max) — W11 ApprovalGate 集成点
- **测试**: 17 secret_manager + 17 mcp_source_tier
- **示例**: `examples/w20_vault.yaml`

### Changed / Migration Notes

#### BREAKING (需 migration)
- `MCPServerConfig` 新增 `source` 字段 (Literal) — YAML 缺此字段 → 启动失败
  ```yaml
  mcp_servers:
    my_server:
      transport: stdio
      command: [...]
      source: community  # 必须显式
  ```
- `SandboxMode` 新增 `DOCKER` 枚举值 — `WORKSPACE_ONLY` 仍是默认 (向后兼容)

#### API 锁定 (Phase 3)
- `TaskQueue.add / claim / complete / fail` — 不变
- `SecurityContext.tenant_id / user_id / mode` — `mode` 新字段
- `Mailbox.send / all_messages` — 不变
- `KB.cache_analysis / get_cached_analysis` — 不变

### 已知问题
- 18 个 sandbox/CLI tests 在 Windows 下 fail (`command not found: [WinError 2]` 是 Windows sandbox subprocess 行为差异, 与 Phase 3 无关, 仅在 Linux runner 通过)

### 性能基准
- 多租户压测: **56723 ops / 10s / 0 越权 / p99=0.5ms** ✅ (100 并发)
- Memory backend: **188467 QPS / p99=0.002ms**
- Redis (fakeredis): **3478 QPS / p99=0.543ms**
- Workspace sandbox: **1308 QPS / p99=15.9ms**
- Docker sandbox (mock): **163 QPS / p99=63ms** (启动开销 50ms ≤ 500ms DoD)

### 测试统计
- W14a-W20 新增/改动测试: **~245 tests 全部 PASS**
- 全量回归: **1077 passed / 35 failed / 1 skipped** (35 fail 是 Windows sandbox 已知差异)

---

## [0.2.0] - 2026-07-15

Phase 2 收尾: 飞书通道 + MCP server 集成 + 多通道 + AgentReview

### Added
- W8-W13 Phase 2 全部交付 (见 git log)

---

## [0.1.0a1] - 2026-06-01

Phase 1 alpha: 核心 swarm + TaskQueue + Mailbox + KB + sandbox 基础

### Added
- W1-W7 Phase 1 全部交付 (见 git log)
