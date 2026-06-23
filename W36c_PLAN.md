# W36c: vault://path#field URI 扩展 (闭环 W36a 协议) PDCA Plan

> PDCA **Plan** 阶段 (2026-06-24)
> 模板见 [`PDCA.md`](PDCA.md)
> 闭环 W36a 已知限制: "vault://path#field URI 留 W36c"
> 衔接: W36a SecretRef 协议 / W26 VaultSecretManager / W20 SecretManager ABC

## 1. 背景 / 闭环目标

**W36a 当前状态:**
- `parse_secret_ref` 识别 literal / `${VAR}` / `secret://key` 三种
- `secret://` 模式走 SecretManager.get(key) (W20/W26 通用接口)
- vault:// 未实现 (W36a 明确留口子)

**W36c 目标:**
- 识别 `vault://path/to/secret#field` URI scheme
- `field` 部分从 JSON 文档中提取 (Vault KV v2 风格, 多个 field 共享一个 path)
- 自动实例化 `VaultSecretManager` (create_app 缺省)
- CLI + Web 集成, 闭环 W36a 协议

## 2. DoD 拆解 (对照 W36a 已知限制 + P5 §17.2)

- [ ] **D1** `parse_secret_ref` 识别 `vault://path#field` 格式
  - `vault://web/jwt` → `SecretRef(kind="vault", value="web/jwt", field=None)`
  - `vault://web/jwt-secret#key` → `SecretRef(kind="vault", value="web/jwt-secret", field="key")`
  - 校验: path 非空, field 非空
- [ ] **D2** `SecretRef` 扩展 `field: str | None` 字段
  - 兼容 W36a: 老测试 (3 kinds) 不破
  - frozen dataclass, 不可变
- [ ] **D3** `JWTIssuer.resolve_secret` 处理 vault:// kind
  - 调 `secret_manager.get(path)` → Secret
  - field 给出时: 解析 JSON, 提取 field
  - field 未给时: 用 value 直接
  - 走 (key, version) cache, version 变化时重读
- [ ] **D4** `create_app` 自动实例化 `VaultSecretManager` (vault:// + 无 secret_manager)
  - 需要 URL / role_id / secret_id 配置
  - 缺参数 → 报错 (不静默, 跟 W36a 一样)
- [ ] **D5** CLI `--web-jwt-secret-ref` 接受 vault://
  - 已有 `--web-secret-manager` 选项复用
  - vault:// 模式: 必须 `--vault-url` / `--vault-role-id` / `--vault-secret-id`
- [ ] **D6** `tests/unit/test_web_jwt_vault_ref.py` ≥6 cases
  - parse `vault://path` (无 field)
  - parse `vault://path#field` (有 field)
  - parse 错误: vault:// 空 path / 空 field
  - SecretRef field 字段
  - JWTConfig vault:// 模式
  - JWTIssuer vault:// resolve_secret (fake VaultSecretManager)
- [ ] **D7** Golden Case G-028 端到端
  - vault://path#field 解析 + resolve
  - field JSON 提取
  - 轮换不重启 (cache 失效)
  - 失败降级 (Vault 不可用, cache 命中)
- [ ] **D8** `tools/verify_w36c_dod.py` 8 项全过
- [ ] **D9** ruff 0 / mypy 0 / 全量 0 新失败 (W36b baseline 1185+)
- [ ] **D10** CHANGELOG W36c 节点 + docs/MEMORY.md + docs/P5-RETRO.md

## 3. 风险登记

| # | 风险 | 缓解 | 状态 |
|---|------|------|------|
| R1 | vault:// 无 field 时与 secret:// 行为重复 | vault:// 必须有 SecretManager, secret:// 默认 EnvSecretManager, 行为有差异 | 🟢 文档化 |
| R2 | field 提取时 JSON 解析失败 | try/except → JWTError, 不破 | 🟡 待 D3 实现 |
| R3 | vault:// 触发自动 VaultSecretManager 实例化, 但生产部署常需自定义 client (hvac 注入) | CLI 显式 `--web-secret-manager vault` 才走 vault:// 自动实例化; 测试可注入 fake | 🟢 模式复用 W36a |
| R4 | path 含 `#` 字符 (Vault path 不允许但防御) | 解析时按第一个 `#` 切, 不允许多个 `#` | 🟡 待 D1 校验 |
| R5 | vault secret 文档是 YAML 不是 JSON | W36c 只支持 JSON (W26 实现风格); YAML 留 W36c+ | 🟢 范围收口 |
| R6 | `field` 是 SecretRef 的新字段, 改动 ABC 影响下游 | 字段 default None, 现有 22 老 test case 不破 | 🟢 向后兼容 |
| R7 | 真实 Vault 不可用测试 | fake VaultSecretManager 模拟 get/put/rotate, 跟 W36a 同一模式 | 🟢 模式复用 |
| R8 | cache 键: vault://path#field 用什么作 cache key? | 用 path 作为 key, field 提取发生在 cache hit 之后 | 🟡 待 D3 决定 |

## 4. 资源 / 预算

- **工时**: ~5 小时 (协议扩展 + 集成 + 测试, 模式高度复用 W36a)
- **关键路径**: D1 (协议) → D2 (SecretRef 字段) → D3 (resolve_secret 扩展) → D4 (create_app) → D6 (单测) → D7 (G-028) → D8 (守门)
- **阻塞条件**: 无 (W26 VaultSecretManager 已有, 模式高度复用 W36a)
- **依赖**: `hvac` 已有 (W26); 无新依赖

## 5. Check 守门点 (本轮 C 阶段必跑)

```bash
# 主守门
.venv/bin/python tools/verify_w36c_dod.py    # 8 项全过

# 标准
.venv/bin/ruff check src tests              # 0 errors
.venv/bin/mypy src/agent_swarm              # Success
.venv/bin/pytest tests/unit -q              # 0 新失败

# Golden
.venv/bin/pytest tests/golden/test_g028_vault_ref.py -v  # 4/4

# 回归
.venv/bin/pytest tests/unit/test_web_jwt_secret_ref.py -v  # W36a 不破
.venv/bin/pytest tests/unit/test_web_jwt_rotation.py -v    # W36a 不破
```

## 6. Act 输出 (本轮 C 通过后必须落)

- [x] `CHANGELOG.md` 新增 W36c 节点 (DoD/数据/差距/Act 段) — commit W36c
- [x] `docs/MEMORY.md` 新增 W36c 经验 (7 条关键经验) — 本地 untrack
- [x] 本地 `docs/P5-RETRO.md` (untrack) W36c 段 — 完成 (做对/做错/风险/数据/MEMORY 链接)
- [x] 不开 tag (W36c 是 P5 中间切片, 0.5.0a2 时再批量打)

**W36c 闭环状态: ✅ 全部 4 项 Act 输出完成,本轮 PDCA 已闭环**

## 7. 下一轮 (W36d) 预告

候选 (W36c 完成后):
- **W36d**: 0.5.0a1 → 0.5.0a2 推进 (dist 重打, CHANGELOG 合并)
- **W36e**: repo 级 `ruff format` 136 欠债 (历史清理)
- **W36f**: agent_review 全模式 (LLM + 对抗式) Web 异步入口

## 8. 引用

- `PDCA.md` — 本轮循环模板
- `CHANGELOG.md` W36a 节点 — SecretRef 协议源头
- `CHANGELOG.md` W26 节点 — VaultSecretManager (KV v2 + 缓存)
- `CHANGELOG.md` W20 节点 — SecretManager ABC
- `src/agent_swarm/security/secret_manager.py` — `VaultSecretManager.get`
- `src/agent_swarm/web/auth.py` — `parse_secret_ref` / `JWTIssuer.resolve_secret` (W36a 实现)
- `DESIGN.md` §17.2 — P5 DoD 源 (本地 untrack)
- `docs/MEMORY.md` W36a 段 — SecretRef 协议经验
