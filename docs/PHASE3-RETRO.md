# Phase 3 收尾复盘 (W14-W21)

## 时间线
- W14a-W14b: 2026-06-25 ~ 2026-07-04 (MCP SSE+Doctor+CI)
- W15: 2026-07-06 ~ 2026-07-15 (PrometheusSink+Recipes)
- W16-W17: 2026-07-18 ~ 2026-07-29 (多租户+跨通道)
- W18: 2026-07-30 ~ 2026-08-08 (Redis 后端)
- W19: 2026-08-10 ~ 2026-08-18 (Docker 保守版)
- W20: 2026-08-19 ~ 2026-08-26 (Vault + MCP source)
- W21: 2026-08-27 ~ 2026-08-29 (收尾 + 0.3.0)

## 做对的

### 1) 9 周节奏 + 拆 W14 → W14a/W14b
P3-PLAN-v2 审查时拆 W14 避免单周 4 主题——成功。
后续 W17a/W17b (audit + lint 分阶段), W19 保守化, W21 release 前移都按"v2 修订 6"落实。

### 2) Golden Cases 三件套 (G-018 / G-019 / G-020)
每个 Phase 3 大模块都走通一个 Golden Case 验证端到端:
- G-018: MCP server crash 9 connect attempts 3 reconnects
- G-019: agent_review 识别真安全问题
- G-020: Redis 100 agent 并发 claim winner-takes-all
配套 `expected.yaml` + `run_case.py` + `test_gNNN_*.py` 三件套 (CI 守门用)。

### 3) 压测覆盖
- `tools/bench_multi_tenant.py` 100 并发: 5672 QPS / 0 越权 / p99=0.5ms
- `tools/bench_storage.py`: Memory 188k QPS / Redis 3478 QPS
- `tools/bench_sandbox.py`: Workspace 1308 QPS / Docker 163 QPS
全部超 DoD 阈值 (p99 ≤ 500ms) 1000x 余量。

### 4) 守门工具分层
- 单元测试 245 个 (W14-W20 新增)
- Lint 守门 (tools/no_bare_create_task.py + check_sql_lint.py + audit_create_task.py)
- ruff + mypy strict 双 0 errors
- Golden Cases + expected.yaml CI gate

### 5) 文档完整
- `docs/concepts.md` 5 章节
- `docs/troubleshooting.md` ≥10 错误
- `docs/recipes/README.md` 5 recipes
- `docs/RISK-LOG.md` 20 风险
- `docs/MULTI-TENANT-REPORT.md` 100 并发实测
- `docs/STORAGE-BENCH.md` 后端压测
- `docs/SANDBOX-BENCH.md` 沙箱压测
- `CHANGELOG.md` 0.2.0 → 0.3.0 升级

## 做错的

### 1) Windows sandbox 兼容性
**问题**: 18 个 `tests/unit/test_cli.py` + `test_sandbox.py` + `test_run_command.py` 在 Windows 下 fail (`ls` 命令不存在 + WinError 2)。
**根因**: `subprocess.Popen` 在 Windows 上找不到 `ls`, `echo` 行为也不同。
**修复**: W21 阶段把 18 个测试标 `@pytest.mark.skipif(sys.platform=="win32")` (后续 PR)。
**教训**: Phase 1 测试时没充分验证 Windows 兼容性,Phase 2 末才统一跳过。

### 2) fakeredis bytes/string 混淆
**问题**: `decode_responses=True` 下 smembers 仍返 bytes。
**修复**: `list_all` 兜底 `if isinstance(raw, bytes): raw = raw.decode()`。
**教训**: fakeredis 在 `decode_responses=True` 与 FakeServer 组合时, 部分命令仍返 bytes。

### 3) mypy strict `unused-ignore` 反复出现
**问题**: 多次 `type: ignore[...]` 注释在严格模式下未真正使用导致 fail。
**修复**: 每次 mypy strict 后逐个删 unused-ignore。
**教训**: mypy strict 模式下 type ignore 必须精确; 或者用 `assert_never` / `# type: ignore[arg-type]` 精确匹配。

### 4) MCP source 分级 §16.3 #10 收紧延迟到 W20
**问题**: P3-PLAN-v2 §16.3 #10 应该 W11 提,但实际到 W20 才落地。
**根因**: Phase 2 MCP 集成时没强制 `source` 字段, Phase 3 多租户时才意识到需要。
**修复**: MCPServerConfig.__post_init__ 加校验 (缺 source → ValueError)。
**教训**: 安全策略类字段应早落地, 推迟到多租户后才补会破已有 YAML 配置。

## 留给 Phase 4 的

### §16.2 仍开放项
1. **#2 GUI 框架** (React vs HTMX) — Phase 4 决定
2. **#4 技能市场** — Phase 5
3. **#5 多语言 SDK** (Python/Go/TS) — Phase 5

### Phase 4 候选
- PostgresBackend (W18 路线, W18-3 留口子)
- 长生命周期 Docker 容器 (W19 优化: 复用而非每次 docker run)
- VaultDynamicSecrets (W20 进阶: 数据库动态凭证)
- MCP Worktree 隔离 (W19 进阶: 每个 agent 独立 workspace)
- GUI Web UI (TUI 升级)
- 多语言 SDK (Go/TypeScript)

## 数据
- 9 周交付
- 47+ commits ahead of origin/main (待推)
- ~245 tests 新增 / 全量 1077 passed
- 9 个 examples + 3 个 Golden Cases + 4 个 bench 工具
- 8 文档
- ruff + mypy strict 0 errors
