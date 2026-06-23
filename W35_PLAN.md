# W35: WebState 跨进程 fan-out (LISTEN/NOTIFY) PDCA Plan

> PDCA **Plan** 阶段 (2026-06-23)
> 模板见 [`PDCA.md`](PDCA.md)
> 闭环 W33b 已知限制: "跨进程 fan-out 需 LISTEN/NOTIFY" (R4 风险)

## 1. DoD 拆解 (对照 W33b CHANGELOG §179-182 + P5 §17.2 阶段门控)

- [x] **D1** `PostgresNotifier` (标准 asyncpg LISTEN/NOTIFY 封装, 零新依赖)
  - `listen()` 启动后台 listener
  - `notify(event_name, session_id, seq, payload, ts)` 触发 NOTIFY
  - `on_notify(callback)` 注册 envelope 回调
  - `close()` 清理
  - `origin_id` (uuid4 hex) 用于 fan-out loop 防护
- [x] **D2** `NotifyEnvelope` 协议 (JSON 编码, 8KB 截断降级)
  - 字段: `origin / seq / event_name / session_id / payload / ts`
  - > 7KB payload 降级为 `{"_truncated": True, "size": N}` 占位
  - 8KB 是 NOTIFY 硬限制
- [x] **D3** `PostgresWebStateStore.attach_notifier(notifier)` 钩子
  - append 写盘后, 自动调 notifier.notify(...)
  - notifier 未挂时, append 行为零变化 (W33b 兼容)
  - 失败仅 log, 不破本地路径
- [x] **D4** `WebState.attach_notifier(notifier)` 集成入口
  - 自动把 notifier 挂到 store (如有)
  - 注册 on_notify 回调 → 把跨进程 envelope 转成本地 EventRecord + 通知本地订阅者
  - 无 store 时仅保存引用 (caller 自管)
- [x] **D5** `create_app` 接受 `enable_cross_process: bool = False`
  - 启用 + DSN → 实例化 PostgresNotifier, 挂 `app.state.web_notifier`
  - 启用但无 DSN → 静默 (向后兼容)
  - lifespan 启动时 `notifier.listen()` + `state.attach_notifier(...)`
  - 退出时 `notifier.close()` (先于 store)
- [x] **D6** CLI 新增 `--web-cross-process/--no-web-cross-process` 选项
  - 默认 False (W28 行为零破坏)
  - 需配合 `--web-postgres-dsn` 才生效
- [x] **D7** G-025 Golden Case: 跨进程 fan-out 端到端
  - A push → bus → B 收到 (用 fake asyncpg bus 模拟"两个进程")
  - 同 origin 过滤 / 多进程 fanout / 顺序通知 (4 cases)
- [x] **D8** `tools/verify_w35_dod.py` 守门 8 项
  - Envelope 协议 / NOTIFY 发出 / origin 过滤 / 跨进程接收 /
    CLI 选项 / DSN 缺省降级 / create_app 集成 / 性能基线 (100 notify < 5s)
- [x] **D9** ruff 0 / mypy 0 / 全量不破 (W34 1295 baseline)
- [x] **D10** ≥ 15 unit cases + 4 G-025 cases (≥ 19 新增)

## 2. 风险登记

| # | 风险 | 缓解 | 状态 |
|---|------|------|------|
| R1 | asyncpg LISTEN 需独占长连接, 与 append 池冲突 | 独立 `notifier_conn`, 与 pool 分离; Pool 模式时复用 _FakeAsyncpgPool 单 conn 模拟 | 🟢 已处理 |
| R2 | NOTIFY 8KB payload 限制 | 协议层 `NOTIFY_PAYLOAD_LIMIT = 7KB`, 超长降级为 `_truncated` 标记 | 🟢 已处理 |
| R3 | 同进程 origin_id 自订阅 → fan-out loop | `origin_id` (uuid4 hex 32 字符), on_notify 时 `if env.origin == self.origin_id: return` | 🟢 已处理 |
| R4 | LISTEN 失败时破坏单进程路径 | `enable_cross_process=False` 默认, notifier.listen() try/except 仅 log | 🟢 已处理 |
| R5 | PostgresNotifier.close 时 listener_task 未 await | 同步方法, 清理 `_listeners` + `_conn.close()` 即可 (asyncpg 是同步回调) | 🟢 N/A |
| R6 | 多 worker (gunicorn/uvicorn workers) 各自 origin, NOTIFY 触达所有 | 这是预期行为 (W35 目标), 文档化 | 🟢 文档化 |

## 3. 资源 / 预算

- **工时**: ~6 小时 (slice 比 W33a/b 小, 主要是协议 + fake bus + 集成)
- **关键路径**: D1 (协议) → D3 (store hook) → D5 (create_app) → D7 (G-025) → D8 (守门)
- **阻塞条件**: 无 (纯代码, asyncpg 已有, 零新依赖)
- **依赖**: `asyncpg` 已有 (W25), `uuid` 标准库

## 4. Check 守门点 (本轮 C 阶段必跑)

```bash
# 主守门
.venv-win/Scripts/python tools/verify_w35_dod.py    # 8 项全过

# 标准
.venv-win/Scripts/ruff check src tests              # 0 errors
.venv-win/Scripts/mypy src/agent_swarm              # Success
.venv-win/Scripts/pytest tests/unit -q             # 0 新失败

# Golden
.venv-win/Scripts/pytest tests/golden/test_g025_cross_process.py -v  # 4/4
```

## 5. Act 输出 (本轮 C 通过后必须落)

- `CHANGELOG.md` 新增 W35 节点 (DoD/数据/差距/Act 段)
- `MEMORY.md` 新增 1 条经验 (LISTEN/NOTIFY 协议 + origin 过滤 + 8KB 限制)
- 本地 `docs/P5-RETRO.md` (untrack) W35 段
- 不开 tag (W35 是 P5 中间切片, 0.5.0a2 时再批量打)

## 6. 下一轮 (W36) 预告

候选:
- W36a: 解决 P5 §17.2 阶段门控剩余限制 (W34 已知: HS256 共享密钥需 SecretManager 轮换)
- W36b: agent_review Web 入口 (UI 按钮 → 触发 Phase 3 review)
- W36c: 0.5.0a1 → 0.5.0a2 推进 (dist 重打, CHANGELOG 合并)
- W36d: repo 级 `ruff format` 136 欠债 (历史清理)

## 7. 引用

- `PDCA.md` — 本轮循环模板
- `CHANGELOG.md` W33b 节点 — 已知限制源头
- `src/agent_swarm/web/store.py` — W33b 持久化基线
- `src/agent_swarm/web/state.py` — WebState push_event 双写路径
- `tools/verify_w33_dod.py` — 守门脚本模板
