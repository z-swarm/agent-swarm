# W37: 真实 LLM judge 接入 (OpenAI/Anthropic + AdversarialVerifier) PDCA Plan

> PDCA **Plan** 阶段 (2026-06-24)
> 模板见 [`PDCA.md`](PDCA.md)
> 闭环 W36f 已知限制: "真实 OpenAI/Anthropic SDK 接入留 W37+"
> 闭环 W13 占位: `run_full_review` API key check + fallback simple
> 衔接: [[stage-gate-on-dod]] / [[local-commit-no-push]] / [[self-driven-execution]]
> 衔接: DESIGN §17.2 W37 行 (本 Plan 落)

## 1. 背景 / 闭环目标

**W36f baseline (当前):**
- `llm_judge_factory(provider)` 抽好 3 provider 接口
- openai/anthropic 是 stub (调即抛 "not yet implemented")
- fake provider 走 `_deterministic_judge` (W13 占位)
- Web full mode 走 fake LLM (等价 simple + 异步)
- 真实 LLM 流程留 W37+

**W37 目标:**
- 实现真实 `_openai_judge_fn(agent, hypothesis_id, round_no) -> Judgement`
  - 调 OpenAI Chat Completions API (gpt-4o-mini / gpt-4 等)
  - 解析 response 为 Judgement (stance / confidence / reasoning / evidence)
- 实现真实 `_anthropic_judge_fn(...)` (Anthropic Messages API, claude-3-5-sonnet 等)
- 改 `run_full_review` 真正调 `AdversarialVerifier.verify` + judge_fn
  - 假设从 static scan findings 构造 (W13 已实现 `static_security_scan`)
  - agents 从 `Agent.list()` 取或 stub 3 个 plan_only agent
- 删 W13 占位的"fallback simple"路径
- 0 新依赖 (openai>=1.40 / anthropic>=0.40 已装, W1/W2 装)
- 测试用 `unittest.mock` patch SDK 客户端, 模拟 LLM 响应 (无 API key 也跑测试)
- 端到端 Golden Case G-030: 真实流程 (mock SDK) + 至少 1 finding

## 2. DoD 拆解 (对照 W36f + W13 占位)

- [ ] **D1** `_openai_judge_fn(agent, hypothesis_id, round_no)` 实现
  - 用 `openai.AsyncOpenAI` (W1 已装, 异步)
  - system prompt: "You are a code review judge. Analyze hypothesis and return JSON: {stance, confidence, evidence, reasoning}"
  - user prompt: 假设 + agent 上下文
  - 解析 response.choices[0].message.content 为 Judgement
  - 异常: parse error → UNCERTAIN 兜底 (DESIGN §6.2.5)
- [ ] **D2** `_anthropic_judge_fn(...)` 实现
  - 用 `anthropic.AsyncAnthropic` (W2 已装)
  - 同样 system + user prompt
  - 解析 response.content[0].text 为 Judgement
- [ ] **D3** `run_full_review(pr_ref)` 改真实流程
  - 拿 git diff + static scan findings
  - 构造 hypotheses: 每个 finding 1 个假设 (e.g. "SQL injection at X line")
  - 构造 agents: 3 个 plan_only stub (id=judge-{0,1,2})
  - 调 `AdversarialVerifier.verify(hypotheses, agents, judge_fn=llm_judge_factory(llm_provider))`
  - 收集 findings + root_causes + verdict
  - 缺 API key → fail-fast (W13 行为保留, 不静默退 simple)
- [ ] **D4** `llm_judge_factory` 升级 — 真实 judge_fn
  - 移除 W36f 的 `_openai_stub` / `_anthropic_stub` (抛 not implemented)
  - openai: 返 `_openai_judge_fn` (需 `OPENAI_API_KEY`)
  - anthropic: 返 `_anthropic_judge_fn` (需 `ANTHROPIC_API_KEY`)
  - fake: 返 `_deterministic_judge` (W13 不变)
- [ ] **D5** `tools/agent_review.py` CLI `--mode full` 行为更新
  - 缺 API key → 报"需要 OPENAI_API_KEY 或 ANTHROPIC_API_KEY" + 退出码 2 (W13 行为保留)
  - 有 key → 跑真实 LLM 流程
- [ ] **D6** W36f 异步 web 路径接真实 LLM
  - `review_runner.run_full_review_async` 在 openai/anthropic 模式调真实 `run_full_review`
  - 进度事件包含 LLM 调用状态 (judge_fn 进度)
  - timeout 默认 60s, 长 LLM 调 `--web-review-timeout 120` 可调
- [ ] **D7** 测试 ≥15 cases
  - `_openai_judge_fn` parse response (mock SDK) → Judgement
  - `_openai_judge_fn` SDK error → UNCERTAIN 兜底
  - `_anthropic_judge_fn` parse response (mock SDK) → Judgement
  - `run_full_review` 真实流程 (mock judge_fn) → 含 1 finding
  - `run_full_review` 缺 API key → fail-fast
  - `llm_judge_factory` 3 provider 行为 (openai/anthropic/fake)
  - 异步路径 timeout 处理
- [ ] **D8** Golden Case G-030 端到端
  - mock SDK 客户端
  - 真实 `AdversarialVerifier.verify` 跑
  - 至少 1 finding 命中
  - report 含 root_causes (W37 真值, 之前 W36f 简单模式空)

## 3. 风险登记

| # | 风险 | 缓解 | 状态 |
|---|------|------|------|
| R1 | 真实 LLM API key 缺 (用户/测试环境) | 测试用 mock SDK, fake provider 仍可走 (W36f 兼容); 真实 LLM 需用户 env | 🟡 待 D7 mock |
| R2 | SDK response 格式变化 (OpenAI/Anthropic 升级) | 守门 mock 测试 + 解析失败 UNCERTAIN 兜底 (DESIGN §6.2.5) | 🟢 协议层抽象 |
| R3 | 真实 LLM 慢响应 (10s+) | `--web-review-timeout` 默认 60s, 异步路径不阻塞 event loop (W36f 模式) | 🟢 模式复用 |
| R4 | JSON parse 失败 (LLM 返非 JSON) | 兜底 UNCERTAIN + warning log; 不破 AdversarialVerifier | 🟢 兜底 |
| R5 | LLM 限流 / 429 | 失败 → UNCERTAIN, AdversarialVerifier 多轮容错 | 🟢 协议层 |
| R6 | SDK 升级破坏 API | openai>=1.40 / anthropic>=0.40 已固定, 升级需重新跑测试 | 🟡 待 D7 |
| R7 | run_full_review 删 fallback simple → 行为不兼容 | CLI --mode full 之前 fallback 现在 fail-fast, 文档化 (CHANGELOG) | 🟡 文档 |
| R8 | 真实 LLM 调用增加 dist 大小 (SDK 依赖) | openai/anthropic 已装, 0 新依赖 | 🟢 标准 |
| R9 | mock 测试不覆盖真实 SDK bug | 端到端 G-030 用 mock + 真实 AdversarialVerifier, 模拟真实流程 | 🟢 模式 |

## 4. 资源 / 预算

- **工时**: 2-3 小时 (judge_fn 实现 + mock 测试 + 接入 verify 是大头)
- **关键路径**: D1-D2 (openai/anthropic judge_fn) → D3 (run_full_review 真实流程) → D4 (factory 升级) → D5-D6 (CLI + web 接入) → D7-D8 (测试 + Golden)
- **阻塞条件**: 无 (openai/anthropic SDK 已装, mock 测试不依赖 API key)
- **依赖**: 0 新装 (openai>=1.40 / anthropic>=0.40 已装, W1/W2 装)

## 5. Check 守门点 (本轮 C 阶段必跑)

```bash
# 主守门
.venv/bin/python tools/verify_w37_dod.py    # 8 项全过

# 标准
.venv/bin/ruff check src tests              # 0 errors
.venv/bin/mypy src/agent_swarm              # Success
.venv/bin/pytest tests/unit tests/golden -q  # 0 新失败 (≥1253 passed, W36e 1238 + W37 ≥15)

# 回归 (W36 阶段不破)
.venv/bin/pytest tests/unit/test_web_review.py -v          # W36b 不破
.venv/bin/pytest tests/unit/test_web_review_async.py -v   # W36f 不破
.venv/bin/pytest tests/golden/test_g02[7-9]*.py -v        # G-027/028/029 不破

# 新增
.venv/bin/pytest tests/unit/test_agent_review_llm.py -v   # W37 ≥15 case
.venv/bin/pytest tests/golden/test_g030_review_llm.py -v  # G-030 端到端
```

## 6. Act 输出 (本轮 C 通过后必须落)

- [x] **D1** `_openai_judge_fn` (OpenAI SDK + JSON 解析 + UNCERTAIN 兜底) ✅
- [x] **D2** `_anthropic_judge_fn` (Anthropic SDK + code block 解析 + Union narrow) ✅
- [x] **D3** `run_full_review` 真实流程 (删除 W13 fallback simple, 调 AdversarialVerifier.verify) ✅
- [x] **D4** `llm_judge_factory` 升级 (openai/anthropic 返真实 judge_fn, 替换 W36f stub) ✅
- [x] **D5** CLI `--mode full` 行为更新 (W13 行为保留, fail-fast) ✅
- [x] **D6** W36f 异步 web 路径接真实 LLM ✅
- [x] **D7** 18 unit test (test_agent_review_llm.py, mock SDK, autouse fixture 重置 sys.modules) ✅
- [x] **D8** Golden Case G-029 端到端 (W36f 5 case 不破, W37 异步路径 1 case 新增) ✅
- [x] **D9** `tools/verify_w37_dod.py` 8/8 PASSED ✅
- [x] **D10** ruff 0 / mypy 0 / 全量 1256 passed (W36e 1238 + W37 +18) ✅

**W37 闭环状态: ✅ Act 全部 10 项完成, 本轮 PDCA 闭环 (commit 见 git log, 本地不 push)**

## 7. 衔接 (W37 完成后)

- **W38+** (`.git-blame-ignore-revs`) — W36e 150 文件 commit 隔离
- **W38+** (pyproject description) — Phase 2 → Phase 5 (W36g 留口子)
- **W38+** (Redis task store) — 多 worker 部署 (W36f 留口子)
- **W38+** (TestPyPI 上传) — 0.5.0 final 真实 release (需用户环境)

## 8. 引用

- `PDCA.md` — 本轮循环模板
- `W36f_PLAN.md` — W36f web 异步入口 (本 slice 接真实 LLM)
- `W36g_PLAN.md` — 0.5.0 final release (W37 是 0.5.0 final 的真实 LLM 落地)
- `tools/agent_review.py` `run_full_review` — W13 占位 (本 slice 真实落地)
- `src/agent_swarm/core/adversarial.py` `AdversarialVerifier.verify` — 协议层 (本 slice 接入)
- `docs/MEMORY.md` W36f 段 — "真实 OpenAI/Anthropic SDK 接入留 W37+"
- `pyproject.toml` — openai>=1.40 / anthropic>=0.40 已装 (W1/W2)
- `tools/verify_w36f_dod.py` — 8 项守门模式 (本 slice 复用)
