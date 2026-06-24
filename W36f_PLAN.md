# W36f: agent_review full mode Web 异步入口 (LLM + SSE) PDCA Plan

> PDCA **Plan** 阶段 (2026-06-24)
> 模板见 [`PDCA.md`](PDCA.md)
> 闭环 W36b 已知限制: "agent_review 同步阻塞 + 占位 fallback simple"
> 衔接: [[stage-gate-on-dod]] / [[local-commit-no-push]] / [[self-driven-execution]]
> 衔接: DESIGN §17.2 W36f 行 (本 Plan 落)

## 1. 背景 / 闭环目标

**W36b baseline (当前):**
- `POST /api/review` 同步调 `run_review_sync` (确定性 Judge, 无 LLM)
- `run_full_review` 占位: 缺 API key 时 fail-fast; 有 key 时仍回退 simple
- 同步阻塞 event loop 风险 + UI 长时间无响应

**W36f 目标:**
- **异步任务**: `POST /api/review` 立即返 `task_id`,后台跑 full mode
- **进度流**: SSE (Server-Sent Events) 实时推进度,前端订阅
- **LLM 集成**: 抽 LLM judge 工厂,支持 OpenAI / Anthropic (fake for test)
- **AdversarialVerifier**: 跑 3 judge × N 假设 (W13 设计的真正落地)
- **新接口**:
  - `POST /api/review` → `{"task_id": "..."}` (202 Accepted)
  - `GET /api/review/{task_id}/events` → SSE stream (progress / log / done)
  - `GET /api/review/{task_id}` → 查状态 + 结果
- **CLI**: `--web-review-mode {simple,full}` 选模式; `full` 默认走异步
- **向后兼容**: `simple` 模式走 W36b 同步路径,零破坏

## 2. DoD 拆解 (对照 W36b DoD + DESIGN §17.2 新增 W36f 行)

- [ ] **D1** `review_runner.py` 抽 `ReviewTask` dataclass + 内存 task store
  - `task_id: str` (uuid4 hex 32)
  - `status: Literal["pending","running","done","error"]`
  - `progress: int` (0-100)
  - `log: list[str]` (进度日志)
  - `result: ReviewReport | None`
  - `error: str | None`
  - `created_at: float` (epoch)
- [ ] **D2** `review_runner.py` 新增 `run_full_review_async(task_id, pr_ref, repo_root, llm_provider)`
  - 用 FastAPI BackgroundTasks 调度
  - 内部用 `asyncio.to_thread` 跑同步 LLM 调用 (避免阻塞 event loop)
  - 进度更新通过 task store 推送 (in-memory)
- [ ] **D3** `review_runner.py` 新增 `llm_judge_factory(provider: str)`
  - 支持 `openai` / `anthropic` / `fake` 三种
  - `fake` 模式: 返回确定性 JudgeFn (W13 AdversarialVerifier 模式)
  - 缺 API key 时 fail-fast (与 W36b run_full_review 一致)
- [ ] **D4** `routes.py` 新增/改 3 个端点:
  - `POST /api/review` 改异步, 返 `{"task_id": "...", "status_url": "..."}` (202)
  - `GET /api/review/{task_id}/events` SSE stream (text/event-stream)
  - `GET /api/review/{task_id}` 查 status + result
  - 鉴权沿用 W36b 模式 (W34 middleware, PROTECTED_PREFIXES)
- [ ] **D5** `routes.py` `/review` 页面 + HTMX 升级
  - 表单提交后 JS EventSource 订阅 SSE
  - 进度条 + 日志流 + 完成后显示结果
  - 兼容 W36b 简单模式 (mode=simple 走同步,W36f 模式不变)
- [ ] **D6** `app.py` 增 `--web-review-mode {simple,full}` CLI 选项
  - 缺省 `full` (W36f 主推,异步)
  - `simple` 走 W36b 同步路径 (兼容)
  - `--web-review-llm {openai,anthropic,fake}` 选 provider
- [ ] **D7** `tests/unit/test_web_review_async.py` ≥10 cases
  - task 创建 / 状态查询 / 进度更新 / SSE 事件格式
  - LLM provider 工厂 (3 种)
  - 异步路径不阻塞 event loop (asyncio.wait_for)
  - 错误处理 (无 API key / LLM 失败 / task 不存在)
- [ ] **D8** Golden Case G-029 端到端
  - `fake` LLM provider 跑 AdversarialVerifier
  - 异步任务从 pending → running → done 全过
  - SSE 事件序列 ≥3 条 (start / progress / done)
- [ ] **D9** `tools/verify_w36f_dod.py` 8 项守门
  - ReviewTask dataclass 字段
  - run_full_review_async 函数存在
  - llm_judge_factory 3 provider
  - 3 个新端点 (POST 改异步 + GET events + GET status)
  - CLI --web-review-mode 选项
  - 10+ 新 unit test
  - G-029 Golden Case
  - ruff 0 + mypy 0 + 全量 0 新失败
- [ ] **D10** ruff 0 + mypy 0 + 全量 1214+ passed (W36d 1204 baseline + 10 新)

## 3. 风险登记

| # | 风险 | 缓解 | 状态 |
|---|------|------|------|
| R1 | LLM API key 缺 (用户环境) | `fake` provider 兜底,测试用 fake; 真实 LLM 需 OPENAI/ANTHROPIC_API_KEY | 🟡 待 D3 实现 |
| R2 | 内存 task store 单进程限制 | W36f 接受, 文档化; 多 worker 留 W37+ Redis 共享 | 🟡 范围收口 |
| R3 | SSE 在 FastAPI ASGI 兼容性 | 用 `sse-starlette` 或自实现 (轻量); 先用自实现保持 0 新依赖 | 🟡 待 D4 决策 |
| R4 | 异步路径阻塞 event loop | LLM 同步调用走 `asyncio.to_thread`; SSE 推送用 `asyncio.Queue` | 🟢 模式复用 W34 |
| R5 | task 不存在 (SSE 404) | GET /events 返 404 + JSON 错误; 前端 fallback | 🟢 标准 |
| R6 | LLM 限流 / 超时 | fake provider 立即返; 真实 LLM 走 `--web-review-timeout` 默认 60s | 🟡 待 D6 决策 |
| R7 | fake LLM 与真实 LLM 行为差异 | fake 模拟 3 judge × N 假设, 返确定性 finding; AdversarialVerifier 协议层一致 | 🟢 协议层抽象 |
| R8 | 异步任务无清理 (内存泄漏) | 任务完成后保留 1 小时 (LRU cache), 超时清理; `cleanup_interval=10min` | 🟡 待 D1 实现 |

## 4. 资源 / 预算

- **工时**: 2-3 小时 (LLM 工厂 + async task store + SSE 是大头)
- **关键路径**: D1-D3 (task store + LLM 工厂) → D4-D5 (3 端点 + HTMX) → D6 (CLI) → D7-D8 (测试 + Golden) → D9-D10 (守门)
- **阻塞条件**: 无 (W36b 已闭环, LLM API 是用户责任)
- **依赖**: 无新装 (用标准库 `asyncio` + `uuid` + `dataclasses`; SSE 自实现不引 sse-starlette)

## 5. Check 守门点 (本轮 C 阶段必跑)

```bash
# 主守门
.venv/bin/python tools/verify_w36f_dod.py    # 8 项全过

# 标准
.venv/bin/ruff check src tests              # 0 errors
.venv/bin/mypy src/agent_swarm              # Success
.venv/bin/pytest tests/unit tests/golden -q  # 0 新失败

# 回归 (W36 阶段不破)
.venv/bin/pytest tests/unit/test_web_review.py -v  # W36b 同步路径不破
.venv/bin/pytest tests/unit/test_web_jwt_*.py -v   # W36a-c 鉴权不破
.venv/bin/pytest tests/golden/test_g02*.py -v      # Golden G-022~028 不破

# 新增
.venv/bin/pytest tests/unit/test_web_review_async.py -v  # 10+ 新 case
.venv/bin/pytest tests/golden/test_g029*.py -v          # G-029 端到端
```

## 6. Act 输出 (本轮 C 通过后必须落)

- [x] **D1** `ReviewTask` dataclass (7 字段) + 内存 task store ✅
- [x] **D2** `run_full_review_async` (asyncio.to_thread + 进度推送) ✅
- [x] **D3** `llm_judge_factory` (3 provider: openai / anthropic / fake) ✅
- [x] **D4** 3 端点 (POST 异步 + GET status + GET SSE) ✅
- [x] **D5** `/review` 页面 JS EventSource 升级 ✅
- [x] **D6** CLI 3 选项 (--web-review-mode/--web-review-llm/--web-review-timeout) ✅
- [x] **D7** 18 unit test (test_web_review_async.py) ✅
- [x] **D8** 5 G-029 端到端 (test_g029_review_async_e2e.py) ✅
- [x] **D9** `tools/verify_w36f_dod.py` 8/8 PASSED ✅
- [x] **D10** ruff 0 + mypy 0 + 1233 passed (W36d 1204 + W36f +29) ✅

**W36f 闭环状态: ✅ Act 全部 10 项完成, 本轮 PDCA 闭环 (commit e9b3c4f, 本地不 push)**

## 7. 衔接 (本 Plan 完成后)

- **W36e** (技术债, 推荐接 W36f 后) — `ruff format` 148 文件欠债
- **W36g** (release, 阻塞) — 0.5.0 final, 等 TestPyPI

## 8. 引用

- `PDCA.md` — 本轮循环模板
- `W36b_PLAN.md` — W36b 简单模式设计 (本 slice 兼容路径)
- `W36_RETRO_PLAN.md` — W36 整阶段归档 (本 slice 是 W37 起点)
- `tools/verify_w36{a,b,c,d}_dod.py` — W36 4 个守门脚本 (模式参考)
- `tools/agent_review.py` `run_full_review` — W13 full mode 占位 (本 slice 真实落地)
- `src/agent_swarm/web/review_runner.py` — W36b 包装层 (本 slice 扩展)
- `src/agent_swarm/web/routes.py` `/api/review` — W36b 同步端点 (本 slice 改异步)
- `docs/MEMORY.md` W36b 段 — "未来 W36f 全模式在 review_runner 加新函数, routes 不动" (部分打破,需 D5 同步改 routes)
