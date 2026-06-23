# agent-swarm PDCA 开发循环

> 2026-06-23 引入 — 后续所有 weekly slice / Phase 收尾均走此循环
> 与 `DESIGN.md` §17.1 / §17.2(本地 untrack)+ `CHANGELOG.md` 配套

## 1. PDCA 四阶段

```
    Plan                Do              Check             Act
┌─────────────┐    ┌─────────────┐    ┌─────────────┐    ┌─────────────┐
│ DoD 拆解    │───▶│ 实施        │───▶│ 守门        │───▶│ 差距分析    │
│ 风险登记    │    │ 边做边状态  │    │ verify_*_dod │    │ 写进下轮    │
│ 资源/预算   │    │ 短 status   │    │ 数据/对比   │    │ Plan        │
│ Check 守门点│    │ commit 不推 │    │ 阶段报告    │    │ 归档成功    │
└─────────────┘    └─────────────┘    └─────────────┘    └─────────────┘
      │                                                       │
      └─────────────────── 不通过则回 Plan ─────────────────────┘
```

每个 weekly slice 必须**四阶段都完成**才能算本轮闭环——任何阶段缺失,本轮不闭环。

## 2. 阶段交付物

### P — Plan(每轮起点)

- **DoD 拆解**:从 `DESIGN.md §17.2` 取本 slice 的 DoD,逐条落到 commit/test/file
- **风险登记**:新增 ≥2 条本 slice 风险 + 缓解
- **资源/预算**:估时 + 关键路径 + 阻塞条件(token/SSH/外部依赖)
- **Check 守门点**:`tools/verify_w<N>_dod.py` 必须存在 + 列出校验清单

### D — Do(实施)

- **任务节点**:`TaskCreate` ≥3 个,按"主交付 / 守门 / 文档"三段
- **每步短状态**:任务切换 `in_progress → completed` 时一句话总结(不进 Plan/Act 的反思)
- **commit 节奏**:按 [[local-commit-no-push]] 本地 commit 不 push
- **失败处理**:Do 阶段失败 → 立即回到 P,不强行冲

### C — Check(守门)

- **必跑**:
  - `tools/verify_w<N>_dod.py` exit 0
  - `ruff check src/ tests/` 0 errors
  - `mypy src/` 0 errors
  - 全量 `pytest tests/ -q` 0 新失败(已知 P3 历史失败 allowlist)
- **数据报告**:
  - 单测增量(本 slice 新增 case 数)
  - 性能基线对比(若适用)
  - Golden Case 通过率
- **判断**:全部 ✓ → 进 Act | 任意 ✗ → 回到 P,重做不通过的 DoD

### A — Act(闭环输出)

- **必须写**:`CHANGELOG.md` 本 slice 节点(已含 DoD/数据/差距/Act 段)
- **必须写**:retro 段(本地 untrack 文档)
- **必须写**:`MEMORY.md` 新增条目(本轮关键经验)
- **归档**:
  - 成功 → tag `w<N>-demo` + 准备下一轮 P
  - 失败 → 不归档,失败点写进下轮 P 的风险登记

## 3. 模板(每轮复制)

### Plan 模板

```markdown
# W<N>: <主题> PDCA Plan

## DoD 拆解(对照 DESIGN §17.2)
- [ ] D1 ...
- [ ] D2 ...
- [ ] D3 ...

## 风险登记
| 风险 | 缓解 | 状态 |
|------|------|------|
| R1 ... | ... | 🟡/🟢/🔴 |

## 资源/预算
- 工时: <X 周 / Y 小时>
- 关键路径: ...
- 阻塞条件: <token / SSH / 外部依赖>

## Check 守门点
- tools/verify_w<N>_dod.py: [ ] 创建  [ ] 12+ 检查
- ruff 0 / mypy 0
- 全量 0 新失败
```

### Check 报告模板

```markdown
# W<N> Check 报告

## 守门结果
- verify_w<N>_dod.py: <pass/fail> (<X>/<Y> ✓)
- ruff: 0 errors
- mypy: 0 errors
- 全量: <N> passed / <M> failed / <S> skipped

## 数据对比
- 单测增量: <+N>
- Golden Cases: <通过/总数>
- 性能: <对比基线>

## 差距(本轮未达成)
- G1 ...
- G2 ...

## Act 决策
- 进下一阶段 (W<N+1>)/ 回 Plan 重做 W<N>
```

### Act 模板

```markdown
# W<N> Act 闭环

## CHANGELOG 节点
- [x] <一段话> (<commit hash>)

## retro 段(本地)
- 做对的: ...
- 做错的: ...
- 风险落地: ...
- 数据: ...

## MEMORY 新增
- <关键经验 + link 到现有 memory>

## 下一轮
- W<N+1> Plan: <链接/概览>
```

## 4. 与现有机制的关系

| 机制 | 关系 |
|------|------|
| DESIGN §17.1 垂直切片 | PDCA 的"切片"= PDCA 循环的一轮 |
| DESIGN §17.2 DoD | PDCA P 阶段拆解的源头 |
| `tools/verify_*_dod.py` | PDCA C 阶段的守门脚本(每周/每阶段一个) |
| `CHANGELOG.md` 节点 | PDCA A 阶段的强制输出 |
| `MEMORY.md` 条目 | PDCA A 阶段的关键经验沉淀 |
| [[local-commit-no-push]] | PDCA D 阶段的 commit 策略 |
| [[self-driven-execution]] | PDCA D 阶段的工作节奏 |
| [[stage-gate-on-dod]] | PDCA C → A 的门禁规则 |

## 5. 失败模式(自检表)

每轮 Act 阶段对照下面 6 条,任意一条 ✗ 即本轮未闭环:

- [ ] P 阶段 DoD 拆解每条对应到具体 commit/test/file
- [ ] D 阶段每步有 `in_progress → completed` 短状态
- [ ] C 阶段 `verify_w<N>_dod.py` exit 0
- [ ] C 阶段 ruff 0 / mypy 0 / 全量 0 新失败
- [ ] A 阶段 CHANGELOG 节点有数据
- [ ] A 阶段 MEMORY.md 新增条目(成功或失败经验)

## 6. 引用

- `DESIGN.md` §17.1 / §17.2 — 切片 + DoD 源(本地 untrack)
- `CHANGELOG.md` — 每轮 Act 阶段输出
- `tools/verify_*_dod.py` — 每轮 C 阶段守门
- `MEMORY.md` — 每轮 A 阶段关键经验
