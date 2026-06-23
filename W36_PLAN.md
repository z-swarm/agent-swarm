# W36a: WebState JWT Secret 走 SecretManager (轮换不重启) PDCA Plan

> PDCA **Plan** 阶段 (2026-06-23)
> 模板见 [`PDCA.md`](PDCA.md)
> 闭环 W34 已知限制 #2: "HS256 共享密钥需 SecretManager 轮换"
> 衔接: [[stage-gate-on-dod]] / [[local-commit-no-push]] / [[self-driven-execution]]

## 1. 背景 / 闭环目标

**W34 baseline (当前):**
- `create_app(jwt_secret: str | None = None)` — secret 是字面值 / `${VAR}` 引用
- 解析走 `resolve_secret_ref(jwt_secret)` → `os.environ` 直读
- **无 SecretManager 集成 / 无轮换机制** — 改 secret 必须重启服务

**W36a 目标:**
- jwt_secret 走 `SecretManager.get(secret_ref)` 模式 (与 W26 VaultSecretManager 一致)
- 轮换不重启服务 (cache TTL 内平滑切换)
- 兼容 `EnvSecretManager` (Phase 1 默认) + `VaultSecretManager` (Phase 3 升级)
- **向后兼容 W34:** `jwt_secret="literal"` 或 `"${ENV_VAR}"` 仍工作 (零破坏)

## 2. DoD 拆解 (对照 W34 CHANGELOG §228-230 + P5 §17.2 阶段门控)

- [ ] **D1** `SecretRef` 协议 — 字符串格式识别
  - `"literal"` → 字面值 (W34 兼容)
  - `"${VAR}"` → env 解析 (W34 兼容)
  - `"secret://key"` → `SecretManager.get("key")` (W36a 新)
  - 解析函数: `parse_secret_ref(ref: str) -> SecretRef(kind: Literal["literal","env","secret_ref"], value: str)`
- [ ] **D2** `create_app` 接受 `secret_manager: SecretManager | None = None` 关键字
  - 缺省 → 自动实例化 `EnvSecretManager` (向后兼容,W34 行为零变化)
  - 给出 → 用传入的 (供测试注入 fake / VaultSecretManager)
  - `secret_manager` 优先于内置 EnvSecretManager
- [ ] **D3** `JWTIssuer` 持有 `SecretManager` + `SecretRef`
  - `JWTConfig(secret_ref: str, secret_manager: SecretManager)`
  - decode 前 `await secret_manager.get(parsed_key)` 拿 secret
  - 内部缓存 `(key, version) → bytes` 避免每次 decode 打 Vault
- [ ] **D4** 轮换支持 — cache 失效机制
  - `SecretMetadata.version` 变化 → cache miss → 重读
  - 测试: `rotate()` 后旧 token 在 cache TTL 内仍 verify,过期后用新 secret verify
  - 降级路径: `secret_manager.get` 失败时,cache 命中 → 继续用;cache miss → `JWTError`
- [ ] **D5** CLI 集成 — `--web-jwt-secret-ref` + `--web-secret-manager {env,vault}`
  - `--web-jwt-secret-ref secret://web/jwt-secret` (W36a 推荐)
  - `--web-secret-manager env` (默认,零配置,W34 兼容)
  - `--web-secret-manager vault --vault-url ... --vault-role-id ... --vault-secret-id ...`
  - `--no-web-jwt` 完全禁用鉴权 (W34 已有)
- [ ] **D6** Golden Case G-026 — 轮换不重启端到端
  - Phase A: 用 `secret_v1` 签发 token → verify OK
  - Phase B: `rotate()` 到 `secret_v2`
  - Phase C: 用 `secret_v1` 签发的 token 在 cache TTL 内 verify (TTL=0 时立即 reject)
  - Phase D: 用 `secret_v2` 签发 → verify OK
  - fake `SecretManager` (in-memory) 模拟轮换
- [ ] **D7** `tools/verify_w36a_dod.py` 守门 8 项
  - SecretRef 协议 (literal/env/secret_ref)
  - EnvSecretManager 集成 (W34 兼容)
  - VaultSecretManager 集成 (fake vault client)
  - 轮换不重启 (cache 失效)
  - CLI 选项 / secret_manager 注入 / decode 性能 / 全量不破
- [ ] **D8** ruff 0 / mypy 0 / 全量不破 (W35 baseline 1313+)
- [ ] **D9** ≥ 15 unit cases + 4 G-026 cases
- [ ] **D10** `CHANGELOG.md` W36a 节点 + `docs/MEMORY.md` 经验 + `docs/P5-RETRO.md` retro 段

## 3. 风险登记

| # | 风险 | 缓解 | 状态 |
|---|------|------|------|
| R1 | `SecretManager.get` 失败时降级策略不清 | cache 命中 → 继续用;cache miss → `JWTError` (硬错,不静默) | 🟡 待 D3 实现确认 |
| R2 | Vault 不可用时启动失败 | `--web-secret-manager env` 默认 (W34 兼容路径),`--web-secret-manager vault` 时启动期 get 失败 → 明确报错退出 | 🟡 待 D5 CLI 验证 |
| R3 | 轮换期间 token 行为不一致 (cache TTL) | cache TTL 默认 5 分钟 (与 W20 VaultSecretManager 一致),文档化 SLA | 🟢 设计对齐 |
| R4 | `secret://` vs `vault://` 协议混淆 | W36a 只做 `secret://`,`vault://` 留 W37 (避免一刀切) | 🟢 范围收口 |
| R5 | CLI 注入的 `secret_manager` 生命周期 vs lifespan | 不挂 `app.state`,只在 `create_app` 内部用 (用完即放,SecretManager 自管 cache) | 🟢 设计对齐 |
| R6 | 每次 decode 都打 `SecretManager.get` 性能问题 | cache `(key, version) → bytes`,SecretManager 自带 cache + version 校验 → 命中即返 | 🟢 复用 W20 cache |
| R7 | `EnvSecretManager` 异步接口与 FastAPI 同步路径冲突 | FastAPI 0.110+ 全 async 路径,`decode` 已 async,无阻塞问题 | 🟢 已分析 |
| R8 | W34 现有 `resolve_secret_ref` 是否废弃 | 不废弃,扩展: `parse_secret_ref` 内部仍调 `resolve_secret_ref` 处理 env 模式 | 🟢 向后兼容 |

## 4. 资源 / 预算

- **工时**: ~8 小时 (比 W35 略大,涉及 SecretManager 集成 + 协议扩展 + Golden Case)
- **关键路径**: D1 (SecretRef 协议) → D2 (create_app 注入) → D3 (JWTIssuer 重构) → D4 (轮换 cache) → D6 (G-026) → D7 (守门)
- **阻塞条件**: 无 (复用 W26 SecretManager,W34 auth 框架,无新依赖)
- **依赖**: `hvac` 已有 (W26,W20 可选);`secret://` 解析零依赖

## 5. Check 守门点 (本轮 C 阶段必跑)

```bash
# 主守门
.venv-win/Scripts/python tools/verify_w36a_dod.py    # 8 项全过

# 标准
.venv-win/Scripts/ruff check src tests              # 0 errors
.venv-win/Scripts/mypy src/agent_swarm              # Success
.venv-win/Scripts/pytest tests/unit -q              # 0 新失败

# Golden
.venv-win/Scripts/pytest tests/golden/test_g026_jwt_rotation.py -v  # 4/4

# 回归
.venv-win/Scripts/pytest tests/unit/test_web_jwt_auth.py -v   # W34 不破
```

## 6. Act 输出 (本轮 C 通过后必须落)

- [x] `CHANGELOG.md` 新增 W36a 节点 (DoD/数据/差距/Act 段) — commit fff1823
- [x] `docs/MEMORY.md` 新增 W36a 经验 (7 条关键经验) — 本地 untrack
- [x] 本地 `docs/P5-RETRO.md` (untrack) W36a 段 — 完成 (做对/做错/风险/数据/MEMORY 链接)
- [x] 不开 tag (W36a 是 P5 中间切片, 0.5.0a2 时再批量打)

**W36a 闭环状态: ✅ 全部 4 项 Act 输出完成,本轮 PDCA 已闭环**

## 7. 下一轮 (W36b) 预告

候选 (W36a 完成后):
- W36b: agent_review Web 入口 (UI 按钮 → 触发 Phase 3 review)
- W36c: `vault://path#field` URI 扩展 (W36a 留口子)
- W36d: 0.5.0a1 → 0.5.0a2 推进 (dist 重打, CHANGELOG 合并)
- W36e: repo 级 `ruff format` 136 欠债 (历史清理)

## 8. 引用

- `PDCA.md` — 本轮循环模板
- `CHANGELOG.md` W34 节点 — 已知限制源头 (HS256 共享密钥)
- `CHANGELOG.md` W35 节点 — 上一轮闭环 (跨进程 fan-out)
- `src/agent_swarm/web/auth.py` — W34 baseline (HS256 + resolve_secret_ref)
- `src/agent_swarm/security/secret_manager.py` — W26 SecretManager (EnvSecretManager / VaultSecretManager)
- `DESIGN.md` §17.2 — P5 DoD 源 (本地 untrack)
- `docs/MEMORY.md` W35 段 — 上一轮经验
