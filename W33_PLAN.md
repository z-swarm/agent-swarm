# W33: WebState Postgres 持久化 PDCA Plan

> PDCA **Plan** 阶段(2026-06-23)— 决策锁定(2026-06-23):W33 后端 = **Postgres**
> 模板见 [`PDCA.md`](PDCA.md);上游:`PHASE5-PLAN.md §6` 决策表 + §7 选型概要(本地 untrack)

## 1. DoD 拆解(对照 CHANGELOG 0.5.0a1 决策锁定段 + P5 §17.2 缺口)

- [ ] **D1** `WebStateStore` 协议 + `WebStateStorePostgres` 实现,匹配现有 `WebState` 内存 API
  - `append(event_name, session_id, seq, payload) -> None`
  - `recent(n: int, session_id: str | None = None) -> list[EventRecord]`
  - `subscribe(callback)` 与内存版语义一致(单进程内存 dict;不跨进程 fan-out,这是 P5 §17.2 已知限制)
- [ ] **D2** Schema + 索引落地
  - `webstate_events(seq BIGSERIAL PK, ts TIMESTAMPTZ DEFAULT now(), event_type TEXT, payload JSONB, session_id TEXT, tenant_id TEXT DEFAULT 'local')`
  - `idx_webstate_ts (ts DESC)` / `idx_webstate_session (session_id, seq)` / `idx_webstate_tenant (tenant_id, ts DESC)`
- [ ] **D3** 复用 `PostgresBackend`(W25)asyncpg 池,`WebStateConfig.postgres_dsn` YAML 字段;DSN 未配 → 维持现内存 WebState(零破坏向后兼容)
- [ ] **D4** W31 `WebStateSink` 兼容:把 `consume()` 内部从 `web_state.push_event` 改为 `web_state.append` 路径;**或**保留 `push_event` 内存路径,新增 `WebStateStore` 抽象在 sink 层做转发
- [ ] **D5** W31 CLI `--web` 选项链:新增 `--web-postgres-dsn` CLI 选项(与 `--web-host/--web-port/--web-worktree-*` 同级)
- [ ] **D6** 单测 ≥15 cases:`WebStateStorePostgres` 端到端(fakeredis 风格用 `pg_tmp` 内存表 或 mock asyncpg)
- [ ] **D7** G-023 Golden Case(本轮新加):WebState 持久化端到端——进程 A push → kill → 进程 B reconnect → recent 拉回全部
- [ ] **D8** `tools/verify_w33_dod.py` 守门(8 项):表创建/append/recent/subscribe/重启恢复/CLI 选项/DSN 缺省降级/性能基线
- [ ] **D9** ruff 0 / mypy 0 / 全量 0 新失败(G-022 不破)

## 2. 风险登记

| # | 风险 | 缓解 | 状态 |
|---|------|------|------|
| R1 | 真 Postgres 不可用(本地无 PG)| `pg_tmp` 内存 PG(类似 fakeredis 模式)+ `pytest-postgresql` 插件;或 mock asyncpg 走 W25 `fake_module` 同模式 | 🟡 待 D6 决策 |
| R2 | `web_state.push_event` 既有 6 处调用方(W31/W32/G-022 测)需保持兼容 | D4 选"保留 push_event + sink 转发"分支,不动业务 API;新增 `WebStateStore.append` 平行通道 | 🟢 已有方案 |
| R3 | DSN 暴露风险(YAML 里明文)| 沿用 W20 `${VAR}` SecretManager 引用约定,README 同步;`SecretManager.get(secret_ref)` 解析 | 🟡 与 R1 同 D6 决策 |
| R4 | 跨进程 subscribe fan-out 不可行(PG 没法 in-memory pub/sub)| P5 §17.2 阶段门控已承认:本轮只保证"重启不丢事件",实时 fan-out 仍单进程;W34 + 后续可加 LISTEN/NOTIFY | 🟢 文档化 |
| R5 | 性能:append 一次 PG roundtrip,需 batching | `WebStateStore.append` 内置 asyncio.Lock + 后台 flush 协程(50ms 攒批) | 🟡 D1 设计时定 |

## 3. 资源 / 预算

- **工时**:1-2 周(80-120 工时),建议拆 W33a(Schema + Store 协议 + 单测)/ W33b(CLI 集成 + G-023 + 守门 + 文档)
- **关键路径**:D1(协议) → D2(Schema) → D6(单测,可并行 R1 决策) → D4(兼容层) → D7(Golden) → D8(守门)
- **阻塞条件**:
  - 无(代码层自包含,不需 token/SSH)
  - 仅 CI 需 `pytest-postgresql` 或 docker postgres,GH Actions 可加 service container
- **依赖**:`asyncpg`(已有,W25 用过)/ `pytest-postgresql` 或 `pg_tmp`(测试用,新加)

## 4. Check 守门点(本轮 C 阶段必跑)

```bash
# 主守门
.venv/bin/python tools/verify_w33_dod.py    # 8 项全过

# 标准
.venv/bin/python -m ruff check src/ tests/   # 0 errors
.venv/bin/python -m mypy src/                # Success
.venv/bin/python -m pytest tests/ -q         # 0 新失败(已知 P3 4 个 allowlist)

# Golden
.venv/bin/python -m pytest tests/golden/test_g023_webstate_persistence.py -v  # G-023 3/3
```

## 5. Act 输出(本轮 C 通过后必须落)

- `CHANGELOG.md` 新增 `## [0.5.1a1] - 2026-07-XX` 节点(W33 段含 DoD/数据/性能/G-023)
- 本地 `docs/P5-RETRO.md`(untrack)W33 段
- `MEMORY.md` 新增 1 条经验(预计:Postgres 持久化取舍 / asyncpg 复用 / G-023 设计要点)
- git tag `w33-demo`(成功时)或不开 tag(失败回 Plan)

## 6. 下一轮(W34)预告

W34 = JWT 鉴权(决策锁定);DoD 入口见 `CHANGELOG 0.5.0a1` 决策锁定段。
W33 → W34 衔接点:`create_app` 接受 `WebState` 参数(已 W32 落地),W34 在 `create_app` 上加 `JWTIssuer` + middleware;`Depends(get_current_user)` 与 `web_state.append` 正交,无侵入。

## 7. 引用

- `PDCA.md` — 本轮循环模板
- `PHASE5-PLAN.md`(untrack)§6 决策 + §7 选型概要
- `CHANGELOG.md` 0.5.0a1 节点 — 决策锁定 + DoD 源头
- `src/agent_swarm/web/state.py` — 现有 WebState 实现
- `src/agent_swarm/observability/web_state_sink.py` — 现有 sink
- `src/agent_swarm/core/backends/postgres_backend.py` — W25 复用入口
- `tests/golden/test_g022_web_ui_e2e.py` — G-022 基线(不破)
