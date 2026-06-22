# Changelog

All notable changes to agent-swarm will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.5.0a1] - 2026-06-22

### Phase 5 启动 (W28 GUI Web UI v1)

#### W28: GUI Web UI v1
- **新增**: `src/agent_swarm/web/` FastAPI 应用 (5 文件)
  - `app.py` — `create_app()` 工厂 + lifespan + 路由注册
  - `state.py` — `WebState` 全局状态 (事件缓冲 + 订阅者)
  - `routes.py` — 4 页面 + 5 partials + 3 JSON API
  - `websocket.py` — `/ws` 实时事件流 (心跳 + 自动重连)
  - `__init__.py` — 导出
- **新增**: 12 个 Jinja2 模板
  - `base.html` — HTMX 2.0 启用, nav + WS status
  - 4 页面: dashboard / agents / worktrees / tasks
  - 5 partials: events / metrics / agents / worktrees / tasks
- **新增**: 静态资源
  - `style.css` — 暗色主题 (CSS vars)
  - `app.js` — WebSocket client (重连 + 渲染)
- **新增**: pyproject.toml `[web]` optional-deps
  - fastapi>=0.110, uvicorn>=0.27, jinja2>=3.1, python-multipart>=0.0.9
- **新增**: `examples/w28_web_demo.yaml` — 2 worker + web UI
- **API**:
  - `GET /` / `/agents` / `/worktrees` / `/tasks` — HTML 页面
  - `GET /partials/{events,metrics,agents,worktrees,tasks}` — HTMX fragments
  - `GET/POST /api/{state,events}` — JSON
  - `GET /healthz` / `/metrics` (Prometheus 格式)
  - `WS /ws` — 实时事件流 (推所有 push_event)
- **测试**: 29 cases (app 工厂 / 12 GET 路由 / 5 API / 3 WS / 4 WebState)

#### 启动方式
```bash
pip install -e ".[web]"
uvicorn agent_swarm.web:app --reload
# 浏览器打开 http://localhost:8000
```

#### 已知限制 (v1)
- 无 RBAC / auth
- 事件缓冲默认 500 条 (超出丢老)
- Worktree 视图需 app.state.worktree_manager 注入 (P4-W22 集成入口预留)
- HTMX 自动刷新, 不支持手动控制

#### W31: Web UI CLI 集成 (--web 启动 uvicorn + WebStateSink)
- **新增**: `src/agent_swarm/observability/web_state_sink.py` — `WebStateSink(ObservabilitySink)`
  - consume SessionEvent 推入 WebState (驱动 Web UI)
  - 异常内部吞掉 (warning log), 不影响其他 sink
  - `drop_unsupported=False` 默认全推
- **新增**: `tests/unit/test_web_state_sink.py` — 10 cases
  - 基本 push / 空 payload / 嵌套 payload
  - 与 ObservabilityBus 集成 / 多 sink 共存
  - sink 异常不传播 / bus 兜底 / unregister / 协议 / repr
- **修改**: `src/agent_swarm/observability/__init__.py` — 导出 `WebStateSink`
- **修改**: `src/agent_swarm/cli/main.py` (+67) — `run` 命令新增选项
  - `--web` 启用 Web UI (同进程 uvicorn)
  - `--web-host` 绑定地址 (默认 127.0.0.1)
  - `--web-port` 端口 (默认 8000)
  - import 失败 (未装 [web]) → 友好提示 + sys.exit(2)
  - finally 块: `web_server.should_exit=True` + 等待 web_task 干净关闭
- **新增**: `examples/w31_web_with_swarm.yaml` — writer + reviewer 2 worker + `--web` 启动示例

#### 启动方式 (W31)
```bash
pip install -e ".[web]"
agent-swarm run examples/w31_web_with_swarm.yaml --web
# 浏览器打开 http://localhost:8000
```

#### DoD 验证 (W31)
- ruff 0 / mypy 0
- 10 cases (test_web_state_sink) passed
- 49 cases (test_web + test_web_state_sink + test_websocket_sink) 全过
- CLI `--help` 显示 `--web` / `--web-host` / `--web-port`
- 端到端: `web ui started (uptime=0s)` + LLM 失败时干净退出

## [0.4.0a1] - 2026-06-22

### Phase 4 收尾 (W22-W26)

#### W22: WorktreeManager 核心 (manager.py)
- **新增**: `WorktreeManager(repo_root, base_dir)` ——per-agent git worktree 隔离
- **新增**: `WorktreeHandle` dataclass (path / branch / agent_id / tenant_id / session_id / created_at / key)
- **API**: `acquire(tenant_id, session_id, agent_id) → WorktreeHandle` (幂等)
- **API**: `release(handle, force=False)` / `list_active()` / `get(key)` / `cleanup_orphans(ttl)` / `cleanup_all()`
- **异常**: `WorktreeError` / `WorktreeRepoError` / `WorktreeConflictError`
- **并发安全**: per-tenant `threading.Lock` + 同 (tenant, session, agent) 幂等
- **测试**: 26 cases (基础 / 并发 / 隔离 / cleanup / 路径安全)
- **路径处理**: `_sanitize` 把任意字符串转安全标识符; `_is_git_repo` 跨平台 (Windows 正反斜杠归一)

#### W23: MCP 集成 (integration.py)
- **新增**: `${WORKTREE_PATH}` 占位符 → 注入 `MCPServerConfig.command` / `cwd` / `env`
- **新增**: `substitute_placeholders` / `validate_config` / `find_placeholders`
- **新增**: `WorktreeIntegration(manager)` 高层封装
- **新增**: `examples/w22_mcp_worktree.yaml` ——2 worker 共享 repo
- **新增**: G-021 Golden Case 3 cases (3 agent 100 文件 / 10 并发 / 跨租户)
- **新增**: `tools/bench_worktree.py` 压测
- **新增**: `tools/verify_p4_dod.py` DoD 守门

#### W24: Docker 长生命周期 (sandbox_docker.py)
- **新增**: `DockerConfig.long_lived: bool = True` (默认)
- **新增**: `_start_container` / `_stop_container` / `_run_in_long_lived_container`
- **新增**: `close()` / `__aenter__` / `__aexit__` 异步 context manager
- **容器名**: `agentswarm-<workspace_hash>-<pid>-<counter>` (类级计数器防冲突)
- **性能**: 100 execute() 只启 1 容器 (vs W19 模式 100 次)
- **测试**: 13 cases (CIS 参数 / 启停 / 续用 / 兼容 W19 / 容器名唯一)
- **兼容**: `long_lived=False` 保留 W19 行为

#### W25: PostgresBackend (postgres_backend.py)
- **新增**: `PostgresBackend` + `PostgresConfig` 生产级持久化后端
- **Schema**: `tasks(id PK, version INT, data JSONB, updated_at TIMESTAMPTZ)`
- **CAS**: `UPDATE ... WHERE id=? AND version=? RETURNING data` (单语句原子)
- **命名空间**: schema 隔离
- **连接池**: asyncpg (min_size=1, max_size=20)
- **测试支持**: `fake_module` 参数注入 mock
- **测试**: 13 cases (CRUD / CAS / 重复 / stats / close / 协议)
- **集成**: `backends/__init__.py` 加导出 (try/except 可选依赖)

#### W26: Vault Dynamic Secrets (secret_manager.py)
- **新增**: `DBCredentials` dataclass (username / password / lease_id / lease_duration / issued_at)
  - 派生属性: `expires_at` / `seconds_to_expiry` / `is_expired` / `as_dsn()`
- **新增**: `VaultDynamicSecretManager(VaultSecretManager)`
  - `get_dynamic_credentials(role)` — Vault database/creds/{role} 发凭证
  - `renew_lease(lease_id, increment=3600)` — 续约
  - `revoke_lease(lease_id)` / `revoke_all()` — 显式回收
  - `list_active_leases()` — 调试/监控
- **用例**: 连接 DB 前 get → 用完 revoke / 长期任务 renew
- **测试**: 14 cases (DBCredentials / get / renew / revoke / workflow)

#### Phase 4 统计
- unit tests: 1060 passed (was 975 in P3, +85 new)
- golden: G-021 3/3 (新增)
- 全量: 1060 passed / 138 P3-WIN skipped / 0 failed
- ruff 0 errors / mypy 0 errors
- 4 commits (W22-23, W24, W25, W26) + 4 tags (0.4.0a1-0.4.0a4)

#### 待启动 (Phase 4 续 / Phase 5)
- GUI Web UI (§16.2 #2 React vs HTMX 选型) - 4-6 周
- 多语言 SDK (Go/TypeScript) - 4 周
- 分布式 swarm (跨机器调度) - 长期

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
