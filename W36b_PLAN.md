# W36b: agent_review Web 入口 (UI 按钮触发 review) PDCA Plan

> PDCA **Plan** 阶段 (2026-06-24)
> 模板见 [`PDCA.md`](PDCA.md)
> 衔接: W36a 闭环 / W13 7 类规则 / W34 写路径鉴权
> 衔接: [[stage-gate-on-dod]] / [[local-commit-no-push]] / [[self-driven-execution]]

## 1. 背景 / 闭环目标

**当前状态:**
- `tools/agent_review.py` 提供 `run_simple_review(pr_ref) → ReviewReport` 同步 API
- W13 7 类规则: secret_leak / cmd_injection / path_traversal / eval / sql_injection / data_exposure / weak_hash
- 用户目前只能 CLI 调: `python tools/agent_review.py --pr main..HEAD`
- Web UI (P5) 没有 review 入口 → 用户需在终端跑, 体验断裂

**W36b 目标:**
- Web UI 加 `/review` 页面 + "Run Review" 按钮 (HTMX)
- `POST /api/review` 接受 `pr_ref` JSON, 调 `run_simple_review`, 同步返 Report
- 写路径强制 Bearer token (W34 模式, 与 `/api/events` 一致)
- 错误处理: 无 git repo / 无 diff / 异常 → 友好 JSON 错误
- Golden Case G-027 端到端 (干净 PR 0 finding / 有问题 PR ≥1 finding)

## 2. DoD 拆解 (对照 W13 DoD + W34 鉴权 + P5 §17.2 阶段门控)

- [ ] **D1** `POST /api/review` 路由
  - 接受 `pr_ref: str` (default `"main..HEAD"`)
  - 调 `run_simple_review(pr_ref)` 同步返
  - 返 `{"ok": true, "report": ReviewReport.to_dict()}` JSON
  - 写路径 → 复用 W34 middleware 401 拦截 (无 token 直接拒)
- [ ] **D2** `/review` 页面 (`templates/review.html`)
  - HTMX 表单: PR ref 输入框 + Run Review 按钮
  - 结果展示 partial: verdict / findings 列表 / summary
  - 复用 base.html 主题 (暗色 + nav)
- [ ] **D3** 导航入口 (base.html 加 `/review` link)
- [ ] **D4** 错误处理
  - 无 git repo (cwd 不是 git) → 500 + `{"detail": "not a git repository"}`
  - 无 diff (pr_ref 无变更) → 200 + 空 findings + verdict=approve
  - git 异常 (subprocess 失败) → 500 + 友好错误
- [ ] **D5** `tests/unit/test_web_review.py` ≥8 cases
  - 无 token POST → 401
  - 有效 token + 默认 pr_ref → 200 + report
  - 有效 token + 自定义 pr_ref → 200 + 调对应 ref
  - 空 body → 200 + 默认 main..HEAD
  - 无效 pr_ref → 错误 (但优雅)
  - 无 git repo → 500 + 友好错误
  - GET /review → 200 (页面)
  - 页面 HTML 含 HTMX 表单
- [ ] **D6** Golden Case G-027 (tests/golden/test_g027_review_e2e.py)
  - Case 1: 干净 PR (无新增 security 问题) → 0 findings, verdict=approve
  - Case 2: 有 secret_leak (新增 hardcoded API key) → ≥1 finding, verdict=request_changes
  - Case 3: 有 cmd_injection (os.system 拼接) → ≥1 finding, severity≥HIGH
  - Case 4: 端到端 POST /api/review → JSON 报告含 summary + findings
- [ ] **D7** `tools/verify_w36b_dod.py` 守门 8 项
  - 路由注册 / 写路径鉴权 / 默认 pr_ref / 自定义 pr_ref
  - 无 git repo 错误 / 干净 PR 0 finding / 有问题 PR ≥1 finding
  - HTMX 表单存在 / nav 入口
- [ ] **D8** ruff 0 / mypy 0 / 全量 0 新失败 (W36a baseline 1185+)
- [ ] **D9** ≥ 8 unit cases + 4 G-027 cases
- [ ] **D10** `CHANGELOG.md` W36b 节点 + `docs/MEMORY.md` 经验 + `docs/P5-RETRO.md` retro 段

## 3. 风险登记

| # | 风险 | 缓解 | 状态 |
|---|------|------|------|
| R1 | `run_simple_review` 调 subprocess 慢, 阻塞 FastAPI event loop | 用 `asyncio.to_thread` 包同步调用; 超时 30s | 🟡 待 D1 实现确认 |
| R2 | 无 git 仓库时 `git diff` 抛异常 | try/except 捕 `subprocess.CalledProcessError` + `FileNotFoundError` → 友好错误 | 🟢 设计对齐 |
| R3 | `pr_ref` 注入 (用户传 `; rm -rf /`) | 用 `shlex.split` + 校验不包含 `;` `&` `\|` shell 危险字符 | 🟡 待 D1 校验 |
| R4 | 巨 PR (1000+ 文件) review 慢 | 限制 review 文件数 (复用 W13 `_is_source_file` 白名单); timeout 30s | 🟢 复用 W13 |
| R5 | HTMX 触发 review 失败时用户看不到错误 | partial 渲染错误状态 (红色 alert) + console 错误 | 🟢 设计对齐 |
| R6 | W34 中间件 401 拦截对 /api/review 是否生效 | 复用 W34 `PROTECTED_PREFIXES` 模式 (加 `("/api/events", "/api/review")` 即可) | 🟢 模式复用 |
| R7 | G-027 干净 PR 选什么? 难找完全 0 finding 的 diff | 用 `git diff main..HEAD` 当前 W36a/W36b commit 历史 (我们已控制 risk 等级) | 🟢 数据可控 |
| R8 | `run_full_review` 异步 LLM 调用 vs `run_simple_review` 同步 | W36b 只接 `run_simple_review` (W13 决策); 全模式留 W36b+ | 🟢 范围收口 |

## 4. 资源 / 预算

- **工时**: ~6 小时 (UI + 路由 + 鉴权 + 集成)
- **关键路径**: D1 (路由 + run_simple_review 集成) → D2 (UI 模板) → D5 (单测) → D6 (G-027) → D7 (守门)
- **阻塞条件**: 无 (复用 W13 工具 + W34 鉴权 + P5 模板)
- **依赖**: `tools/agent_review.py` 已就位 (W13); 无新依赖

## 5. Check 守门点 (本轮 C 阶段必跑)

```bash
# 主守门
.venv/bin/python tools/verify_w36b_dod.py    # 8 项全过

# 标准
.venv/bin/ruff check src tests              # 0 errors
.venv/bin/mypy src/agent_swarm              # Success
.venv/bin/pytest tests/unit -q              # 0 新失败

# Golden
.venv/bin/pytest tests/golden/test_g027_review_e2e.py -v  # 4/4

# 回归
.venv/bin/pytest tests/unit/test_web_review.py -v  # 8+ cases
.venv/bin/pytest tests/unit/test_web_jwt_auth.py -v  # W34 不破
```

## 6. Act 输出 (本轮 C 通过后必须落)

- [x] `CHANGELOG.md` 新增 W36b 节点 (DoD/数据/差距/Act 段) — commit W36b
- [x] `docs/MEMORY.md` 新增 W36b 经验 (6 条关键经验) — 本地 untrack
- [x] 本地 `docs/P5-RETRO.md` (untrack) W36b 段 — 完成 (做对/做错/风险/数据/MEMORY 链接)
- [x] 不开 tag (W36b 是 P5 中间切片, 0.5.0a2 时再批量打)

**W36b 闭环状态: ✅ 全部 4 项 Act 输出完成,本轮 PDCA 已闭环**

## 7. 下一轮 (W36c) 预告

候选 (W36b 完成后):
- **W36c**: `vault://path#field` URI 扩展 (W36a 留口子, 闭环 W36a)
- **W36d**: 0.5.0a1 → 0.5.0a2 推进 (dist 重打, CHANGELOG 合并)
- **W36e**: repo 级 `ruff format` 136 欠债 (历史清理)
- **W36f**: agent_review 全模式 (LLM + 对抗式) Web 异步入口

## 8. 引用

- `PDCA.md` — 本轮循环模板
- `CHANGELOG.md` W13 节点 — 7 类规则源头
- `CHANGELOG.md` W34 节点 — 写路径 401 模式
- `CHANGELOG.md` W36a 节点 — 上一轮 (SecretManager 集成)
- `tools/agent_review.py` — `run_simple_review` API
- `src/agent_swarm/web/routes.py` — 现有路由模式
- `src/agent_swarm/web/templates/base.html` — nav 主题
- `DESIGN.md` §17.2 — P5 DoD 源 (本地 untrack)
- `docs/MEMORY.md` W36a 段 — 上一轮经验
