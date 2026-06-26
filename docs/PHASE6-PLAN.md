# Phase 6 Plan: 1.0.0 production release (W40-W50+)

> 准备时间: 2026-06-24 (W39)
> 衔接: Phase 5 收口 (W38, 0.5.0 final production-ready)
> 衔接: [[stage-gate-on-dod]] / [[local-commit-no-push]] / [[self-driven-execution]]

## 1. 阶段背景

Phase 5 (W28-W38, 11 周, 11 slice + 8 守门脚本) 已完整收口:

- 0.5.0 final production-ready (W36g + W38)
- 11 commit: W36a/b/c/d/e/f + 整阶段归档 + W36g + W37 + W38
- 测试 1256 passed, ruff 0 / mypy 0 / format 0 欠债
- 真实 LLM 接入: OpenAI gpt-4o-mini + Anthropic claude-3-5-sonnet (W37)
- Web UI (FastAPI + HTMX + JWT) + 异步 review (LLM + SSE) (W28-W32 + W36f)
- WebState Postgres 持久化 + 跨进程 fan-out (W33-W35)

## 2. 阶段目标

**1.0.0 production release**: 从 0.5.0 走向 1.0.0, 关键缺口:

1. **多 worker 部署** — 单进程内存 task store 无法横向扩展 (W36f 留口子)
2. **真实分布式** — LISTEN/NOTIFY fan-out 实战 + 多节点验证 (W33b 留口子)
3. **TestPyPI / PyPI 真实发布** — 用户环境 token 准备 + 实战发布 (W38 留口子)
4. **实战验证** — 真实场景跑通, 用户反馈循环
5. **1.0.0 release 准备** — CHANGELOG final + dist + tag + GitHub Release

## 3. 阶段范围 (8-12 周, W40-W50 灵活调整)

### 3.1 W40 候选 (Phase 6 启动第一个 slice)

按 W36/W37/W38 留口子排序:

1. **W40**: Redis task store 真实接入 (W36f 留口子)
   - `agent_swarm/web/state.py` 内存 task store → Redis 后端
   - 多 worker 部署基础 (W41 依赖)
   - DoD: 8/8 守门 (Redis backend + fallback 内存 + 跨进程同步)
   - 工作量: 2-3h

2. **W41**: 真实多 worker 部署 (W33b 留口子)
   - gunicorn/uvicorn workers 配置
   - WebState 跨进程 fan-out 实战验证 (W35)
   - DoD: 8/8 守门 (workers 启动 + 状态同步 + fault tolerance)
   - 工作量: 2-3h

3. **W42**: TestPyPI 真实上传 (W38 留口子)
   - 用户环境 `~/.pypirc` token 准备
   - `twine upload --repository testpypi dist/agent_swarm-0.5.0*`
   - 验证 TestPyPI 页面 + 试装
   - DoD: TestPyPI 页面 + 试装通过
   - 工作量: 30min (用户操作)
   - **阻塞**: 需用户环境 token

4. **W43**: 1.0.0 release 准备
   - version 0.5.0 → 1.0.0-rc1
   - CHANGELOG 1.0.0 节点 (W36-W43 全部累计)
   - dist 重新构建 + twine check
   - DoD: 8/8 守门 (W36g 模式复用 + 1.0.0 版本)

5. **W44+**: 实战验证 + 用户反馈循环
   - 真实用户场景跑通
   - 反馈 → 优先级排序
   - 1.0.0 final release (W50 收口)

### 3.2 弹性

8-12 周范围, 实际进度看用户环境 + 实战反馈。如果 W40-W42 推进顺利, W43-W44 提前;反之延后到 W51-W52。

## 4. 阶段 DoD (1.0.0 收口标准)

- [ ] **D1** Redis task store 真实接入 (W40, 多 worker 部署基础)
- [ ] **D2** 真实多 worker 部署实战 (W41, gunicorn/uvicorn workers)
- [ ] **D3** TestPyPI 真实上传 (W42, 用户环境 token)
- [ ] **D4** 1.0.0 release 准备 (W43, version 升级 + CHANGELOG final)
- [ ] **D5** PyPI 正式发布 (W44, 1.0.0 final, 实战验证)
- [ ] **D6** 全量回归 ≥1300 passed (W38 1256 + Phase 6 ≥50)
- [ ] **D7** ruff 0 / mypy 0 / format 0 欠债
- [ ] **D8** 实战验证 ≥3 个真实场景 (用户反馈闭环)
- [ ] **D9** GitHub Release + dist 附件
- [ ] **D10** 1.0.0 final tag + push

## 5. 候选切片优先级 (W40-W44)

| 优先级 | Slice | 内容 | 工作量 | 阻塞 |
|--------|-------|------|--------|------|
| 🥇 | **W40** | Redis task store 真实接入 | 2-3h | 无 |
| 🥈 | **W41** | 多 worker 部署实战 | 2-3h | 依赖 W40 |
| 🥉 | **W42** | TestPyPI 真实上传 | 30min | 需用户 token |
| 4 | **W43** | 1.0.0 release 准备 | 1-2h | 依赖 W42 |
| 5 | **W44** | 1.0.0 final + 实战验证 | 2-3h | 依赖 W43 |

## 6. 风险 + 衔接

### 6.1 风险登记

| # | 风险 | 缓解 | 状态 |
|---|------|------|------|
| R1 | Redis 部署复杂度 (需 Redis server) | 留 fallback 内存 store, 测试用 fakeredis | 🟡 待 W40 |
| R2 | 多 worker 状态同步 bug | LISTEN/NOTIFY 已用 (W35), 实战验证 | 🟢 已用 |
| R3 | TestPyPI 上传误推 PyPI | `--repository testpypi` 必加, RELEASE.md 强调 | 🟢 文档化 |
| R4 | 用户环境 token 安全 | `~/.pypirc` 600 权限, 不进 git | 🟢 文档化 |
| R5 | 1.0.0 范围蔓延 (YAGNI) | 严格按 D1-D10 守门, 不加新特性 | 🟢 守门 |
| R6 | 实战验证无用户反馈 | 提前联系用户, W44 拉真实场景 | 🟡 待 W44 |

### 6.2 衔接 W36-W38

- W36f 留口子 (单进程 task store) → W40 闭环
- W33b 留口子 (真实分布式) → W41 实战
- W38 留口子 (TestPyPI 上传) → W42 闭环
- Phase 5 release 节奏 (W36d/g) → W43 复用
- Phase 5 守门 8 项模式 (W36a-d) → W40-W43 复用

## 7. 已知缺口 (1.0.0 之前处理)

- DESIGN.md / docs/ untrack 设计文档 (W23 已知, 不处理)
- 端到端 e2e (W36g 已知, W44 实战验证)
- multi-tenant 完整版 (W18-W22 已部分, W44 补)
- 真实 LLM 评估 (W37 已落, W44 实战)

## 8. 引用

- `docs/PHASE5-PLAN.md` — Phase 5 计划 (W28 起, 11 slice)
- `W28_PLAN.md` ~ `W38_PLAN.md` — Phase 5 全部 slice
- `CHANGELOG.md` 0.5.0 节点 — 当前 release
- `RELEASE.md` — TestPyPI/PyPI upload 步骤
- `tools/verify_w38_dod.py` — 守门模式
- `tools/verify_p5_dod.py` — P5 守门
- `docs/MEMORY.md` W36-W38 段 — 阶段经验
