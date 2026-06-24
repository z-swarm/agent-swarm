# W36 整阶段 PDCA 闭环归档 (2026-06-24)

> PDCA **Act** 阶段 — 整阶段(4 个 weekly slice)收口归档
> 模板见 [`PDCA.md`](PDCA.md)
> 衔接: [[stage-gate-on-dod]] / [[local-commit-no-push]] / [[self-driven-execution]]
> 衔接: W36a/W36b/W36c/W36d 4 个 slice 全部已闭环

## 1. 背景 / 闭环目标

**W36 整阶段 (4 weekly slice) 范围:**
- **W36a** (`fff1823`) — JWT Secret 走 SecretManager (轮换不重启)
- **W36b** (`ecfbe73`) — agent_review Web 入口 (UI 按钮触发 review)
- **W36c** (`6ca24eb`) — vault://path#field URI 扩展 (闭环 W36a 协议)
- **W36d** (`e7171a6`) — 0.5.0a2 release 推进 (CHANGELOG 合并 + dist + tag)

**整阶段 DoD (对照 DESIGN §17.2):**
- [x] **D1** W36a 8/8 DoD 全过 (`tools/verify_w36a_dod.py`)
- [x] **D2** W36b 8/8 DoD 全过 (`tools/verify_w36b_dod.py`)
- [x] **D3** W36c 8/8 DoD 全过 (`tools/verify_w36c_dod.py`)
- [x] **D4** W36d 8/8 DoD 全过 (`tools/verify_w36d_dod.py`)
- [x] **D5** P5 阶段全量 1342 passed (P5 守门)
- [x] **D6** ruff 0 / mypy 0 (W36a-d 全程)
- [x] **D7** 0.5.0a2 release 完成 (sdist + wheel + twine check + tag)
- [x] **D8** docs/P5-RETRO.md W36 整阶段段落盘 (untrack)
- [x] **D9** docs/MEMORY.md W36 整阶段关键经验段 (untrack, 5 条沉淀)
- [x] **D10** 4 slice × 4 阶段 = 16 个 PDCA 节点全过 (4 commit + 4 守门 + 4 A 段 commit)

## 2. 累计数据 (W36 整阶段)

| 维度 | 数据 |
|------|------|
| 4 slice commit | `fff1823` / `ecfbe73` / `6ca24eb` / `e7171a6` |
| 4 A 段 commit | `4761843` / `94bf26c` / `79c4067` / `259c6de` |
| Files changed | 38 files |
| +insertions | 4113 lines |
| -deletions | 66 lines |
| DoD 总数 | 32/32 全过 (4 × 8) |
| 守门脚本 | 4 个 (`verify_w36{a,b,c,d}_dod.py`) |
| 累计测试 | 1204 → 1342 passed (P5 守门全量) |
| git tag | 0.5.0a2 (新增,W36d) |
| dist | 0.5.0a2 sdist + wheel (W36d) |

## 3. 价值定位

W36 是 P5 中段"WebState 协议收口 + release 节奏成熟"的双重定位:

### 协议层 (W36a/c)
- **W36a**: WebState JWT Secret 从硬编码 → SecretManager (`secret://` 自动 EnvSecretManager)
- **W36c**: 协议扩展 `vault://path#field` URI (VaultManager)
- 1 协议 2 表达, 增量闭环不破老调用 (W36a 3 kinds 兼容)

### 入口层 (W36b)
- agent_review Web 入口: `POST /api/review` + `/review` 页面 + HTMX 表单
- 简单模式先跑通 (确定性 Judge), 留 W36f 升级 full mode (LLM + 异步)

### 节奏层 (W36d)
- 0.5.0a1 → 0.5.0a2 增量 release 模板成熟
- 8 项守门脚本覆盖 release 全链路 (version / CHANGELOG / dist / twine / tag / ruff+mypy / pytest / slice 引用)
- 模式可复用 0.5.0a3 / 0.5.0 final

## 4. PDCA 自我闭环验证 (4 段 × 4 slice = 16 节点)

| Slice | P (Plan) | D (Do) | C (Check) | A (Act) |
|-------|----------|--------|-----------|---------|
| W36a | W36_PLAN.md | 1 commit (fff1823) | verify_w36a_dod.py 8/8 | commit 4761843 + CHANGELOG + MEMORY + RETRO |
| W36b | W36b_PLAN.md | 1 commit (ecfbe73) | verify_w36b_dod.py 8/8 | commit 94bf26c + CHANGELOG + MEMORY + RETRO |
| W36c | W36c_PLAN.md | 1 commit (6ca24eb) | verify_w36c_dod.py 8/8 | commit 79c4067 + CHANGELOG + MEMORY + RETRO |
| W36d | W36d_PLAN.md | 1 commit (e7171a6) | verify_w36d_dod.py 8/8 | commit 259c6de + CHANGELOG + MEMORY + RETRO |

**整阶段 A 段 (本归档):** W36_RETRO_PLAN.md (本文件) + 1 commit 收口

## 5. 模式沉淀 (5 条)

1. **协议收口模式** — 同一需求走多 URI, 守门验"老 kinds 不破"
2. **Web 入口渐进模式** — 简单版先跑通, 占位留升级口子
3. **release 节点模式** — 汇总表 + 引用, 不重写 W detail
4. **PDCA 自我闭环节奏** — 4 slice × 4 阶段 = 16 节点全过
5. **风险分级 + 范围收口** — release 守住 "dist ready" 边界

详见 `docs/MEMORY.md` W36 整阶段段。

## 6. Act 闭环输出

- [x] **D1-D4** 4 slice DoD 8/8 全过
- [x] **D5** P5 阶段守门 1342 passed
- [x] **D6** ruff 0 / mypy 0
- [x] **D7** 0.5.0a2 release 完成
- [x] **D8** docs/P5-RETRO.md W36 整阶段段 (untrack, 已落盘)
- [x] **D9** docs/MEMORY.md W36 整阶段段 (untrack, 已落盘, 5 条沉淀)
- [x] **D10** 16 个 PDCA 节点全过
- [x] **本文件** W36_RETRO_PLAN.md (整阶段归档, 可追踪)
- [x] **本地 commit** (本文件收口, 不 push)

**W36 整阶段 PDCA 闭环状态: ✅ 全部 10 项 Act 输出完成, 整阶段 P→D→C→A 四阶段全过**

## 7. 下一轮 (W37) 候选

按 W36d §7 + 本归档 §3 衔接:

1. **W36f** (功能, 优先) — agent_review full mode (LLM + 对抗式) Web 异步入口
   - 闭环 W13 dogfooding 承诺
   - 不依赖用户环境, 中等工作量
2. **W36e** (技术债, 并行) — `ruff format` 148 文件欠债 (实测数)
   - 1-2h 原子 commit, 单独走
   - 风险: 一次性大改动污染 blame, 建议 .ruff.toml 分批
3. **W36g** (release, 阻塞) — 0.5.0 final
   - 需用户环境 TestPyPI 验证
   - 衔接: W36d release 模式可复用

## 8. 引用

- `PDCA.md` — 整阶段 PDCA 模板
- `DESIGN.md` §17.2 W36a/b/c/d DoD (untrack)
- `CHANGELOG.md` 0.5.0a2 节点 — 7 slice 汇总
- `docs/P5-RETRO.md` W36 整阶段段 — 详细 retro
- `docs/MEMORY.md` W36 整阶段段 — 5 条关键经验
- W36a/b/c/d_PLAN.md — 4 个 slice PLAN 文件
- `tools/verify_w36{a,b,c,d}_dod.py` — 4 个守门脚本
- `tools/verify_p5_dod.py` — P5 阶段守门 (1342 passed)
