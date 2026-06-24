# W39: Phase 6 启动 (PHASE6-PLAN.md + W40 候选确定) PDCA Plan

> PDCA **Plan** 阶段 (2026-06-24)
> 模板见 [`PDCA.md`](PDCA.md)
> 衔接: W38 Phase 5 收口 (0.5.0 final production-ready)
> 衔接: W28 Phase 5 启动模式 (PHASE5-PLAN.md 起, W28-W38 累计 11 slice + 8 守门 + 1256 passed)
> 衔接: [[stage-gate-on-dod]] / [[local-commit-no-push]] / [[self-driven-execution]]

## 1. 背景 / 闭环目标

**当前状态 (W38 收口后):**
- 0.5.0 final 已发 (W36g) + production-ready (W38: pyproject/RELEASE.md/.git-blame-ignore-revs)
- 11 commit (W36a-f + 整阶段归档 + W36g + W37 + W38)
- 测试 1256 passed, ruff 0 / mypy 0 / format 0 欠债
- Phase 5 (W28-W38) 累计 11 周, 0.5.0 final production-ready
- 留口子 (W37+ / W38+ 3 候选):
  1. TestPyPI/PyPI 真实上传 (用户环境 `~/.pypirc` token)
  2. 多 worker / Redis task store (W36f 留口子)
  3. 真实分布式 (W33b 留口子)

**W39 目标 (开 Phase 6):**
- 写 `docs/PHASE6-PLAN.md` 完整 (8-12 周计划 + 候选 + 衔接)
- 确定 W40 第一个具体 slice 候选 (Phase 6 启动后的第一个周切)
- CHANGELOG 节点 (Phase 6 启动)
- docs/MEMORY 段 (Phase 6 计划经验)
- 守门 5 项全过 (PHASE6-PLAN 完整 + W38/W37 baseline 不破)
- 本地 commit (不 push)

## 2. DoD 拆解 (对照 PHASE5-PLAN.md 模式)

- [ ] **D1** `docs/PHASE6-PLAN.md` 写完整
  - 标题: "Phase 6 Plan: 1.0.0 production release (W40-W50)"
  - 阶段背景: 0.5.0 final 收口, Phase 5 累计
  - 阶段目标: 1.0.0 production release (多 worker / 真实分布式 / 实战验证)
  - 范围: 8-12 周 (W40-W50 候选, 灵活调整)
  - 阶段 DoD: 1.0.0 final + 多 worker 部署 + 实战验证 + 用户环境 TestPyPI/PyPI 上传
  - W40 候选 (5-8 个, 优先级):
    1. **W40**: Redis task store 真实接入 (W36f 留口子, 多 worker 部署基础)
    2. **W41**: 真实多 worker 部署 (W33b 留口子, gunicorn/uvicorn workers)
    3. **W42**: TestPyPI 真实上传 (W38 留口子, 用户环境 token)
    4. **W43**: 1.0.0 release 准备 (CHANGELOG final + dist + tag)
    5. **W44+**: 实战验证 + 用户反馈循环
  - 风险 + 衔接 W36-W38
  - 已知缺口 + 1.0.0 收口
- [ ] **D2** PHASE6-PLAN.md 关键内容校验
  - 含 "1.0.0"
  - 含 "W40" 候选
  - 含 "Phase 5" 衔接
  - 含 "TestPyPI" / "Redis" / "多 worker" 关键词 (≥3)
  - 字数 ≥500 (实质内容)
- [ ] **D3** CHANGELOG 节点
  - W39 节点含 "Phase 6 启动"
  - 引用 PHASE6-PLAN.md
  - 列 W40-W44 候选
- [ ] **D4** `tools/verify_w39_dod.py` 5 项守门
  - PHASE6-PLAN.md 存在 + ≥500 字
  - PHASE6-PLAN.md 含 4 关键词 (1.0.0 / W40 / Phase 5 / TestPyPI)
  - CHANGELOG 含 W39 节点
  - ruff 0 / mypy 0
  - W38/W37/W36 baseline 不破 (≥41 case)
- [ ] **D5** ruff 0 / mypy 0 / 全量 1256+ passed (W38 baseline)
- [ ] **D6** 本地 commit (1 原子, 不 push)

## 3. 风险登记

| # | 风险 | 缓解 | 状态 |
|---|------|------|------|
| R1 | PHASE6-PLAN.md 范围过大 (>12 周) | 守门 2 校验"W40 候选" 列表不超过 8 个; 8-12 周弹性 | 🟡 待 D1 |
| R2 | W40 候选优先级错位 | 按 W36/W37/W38 留口子排序 (Redis 优先, 用户上传次之) | 🟢 排序清晰 |
| R3 | Phase 6 计划与 0.5.0 release 节奏冲突 | 0.5.0 final 已发, Phase 6 是新方向, 不冲突 | 🟢 已闭环 |
| R4 | Phase 5 经验未沉淀 | docs/MEMORY.md W36/W37/W38 段已写, Phase 6 直接引用 | 🟢 已有 |
| R5 | W39 Plan 范围过窄 (1 周可做完) | W39 是"开新方向"切片, PHASE6-PLAN 文档 + 1 个 commit 收口 | 🟢 模式 |
| R6 | 守门"≥500 字"过严 | 守门 2 是软约束, 实质内容 ≥500 字 | 🟡 待 D1 |

## 4. 资源 / 预算

- **工时**: 1 小时 (PHASE6-PLAN.md 写 + 守门 + commit)
- **关键路径**: D1 (PHASE6-PLAN.md) → D2 (内容校验) → D3 (CHANGELOG) → D4-D5 (守门 + 全量) → D6 (commit)
- **阻塞条件**: 无 (Phase 5 收口, Phase 6 启动无依赖)
- **依赖**: 0 新装 (现有工具足够)

## 5. Check 守门点 (本轮 C 阶段必跑)

```bash
# 主守门
.venv/bin/python tools/verify_w39_dod.py    # 5 项全过

# 标准
.venv/bin/ruff check src tests tools        # 0 errors
.venv/bin/mypy src/agent_swarm tools/agent_review.py  # Success
.venv/bin/pytest tests/unit tests/golden -q  # 1256+ passed

# W38/W37/W36 回归
.venv/bin/pytest tests/unit/test_web_review.py \
                   tests/unit/test_web_review_async.py \
                   tests/golden/test_g027_review_e2e.py \
                   tests/golden/test_g029_review_async_e2e.py \
                   -q --tb=no  # 41 case
```

## 6. Act 输出 (本轮 C 通过后必须落)

- [x] **D1** `docs/PHASE6-PLAN.md` 完整 (2596 字, 8 章节) ✅
- [x] **D2** PHASE6-PLAN.md 关键内容校验 (4 关键词全找到) ✅
- [x] **D3** `CHANGELOG.md` W39 节点 (Phase 6 启动) ✅
- [x] **D4** `tools/verify_w39_dod.py` 5/5 PASSED ✅
- [x] **D5** ruff 0 / mypy 0 / 全量 1256+ passed (W38 baseline) ✅
- [x] **D6** 本地 commit (1 原子, 不 push) ✅

**W39 闭环状态: ✅ Act 全部 6 项完成, Phase 6 启动, 1.0.0 方向明确 (W40 候选 Redis task store)**

## 7. 衔接 (W39 完成后)

- **W40** (Redis task store 真实接入) — Phase 6 第一个具体 slice
- **W41** (多 worker 部署) — gunicorn/uvicorn workers 实战
- **W42** (TestPyPI 真实上传) — 用户环境 token 准备后
- **W43** (1.0.0 release 准备) — CHANGELOG final + dist + tag
- **W44+** (实战验证 + 用户反馈) — 1.0.0 production 收口

## 8. 引用

- `PDCA.md` — 本轮循环模板
- `docs/PHASE5-PLAN.md` — Phase 5 启动模式 (W28 模式, 本 slice 复用)
- `W36_RETRO_PLAN.md` — W36 整阶段归档 (Phase 5 中段)
- `W37_PLAN.md` — W37 真实 LLM 接入 (0.5.0 final 价值兑现)
- `W38_PLAN.md` — W38 Phase 5 收口 (0.5.0 final production-ready)
- `RELEASE.md` — TestPyPI/PyPI upload 步骤 (W38)
- `CHANGELOG.md` 0.5.0 节点 — 当前 release 内容
- `tools/verify_w38_dod.py` — 6 项守门 (本 slice 5 项简化)
- `tools/verify_p5_dod.py` — P5 守门 (12 项, 模式参考)
