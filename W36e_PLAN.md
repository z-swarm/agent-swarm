# W36e: repo 级 `ruff format` 150 文件欠债清理 (历史 cleanup) PDCA Plan

> PDCA **Plan** 阶段 (2026-06-24)
> 模板见 [`PDCA.md`](PDCA.md)
> 闭环 W33a 已知技术债 #1: "ruff format 136 文件欠债" (实测 W36e 起点 150, 含 W36f 新增)
> 衔接: [[stage-gate-on-dod]] / [[local-commit-no-push]] / [[self-driven-execution]]
> 衔接: DESIGN §17.2 (本 slice 收口, 无新增 W 行, 仅清欠债)

## 1. 背景 / 闭环目标

**当前欠债 (2026-06-24 实测):**
- `ruff format --check src tests` → 150 files would be reformatted, 35 already formatted
- 累计欠债历史: W33a 已知 136 → W36f 收尾 150 (W36a/b/c/d/f 阶段新增 14 个文件未格式化)
- ruff check 0 + mypy 0 + 全量 1233 passed (W36f baseline)

**W36e 目标:**
- 1 原子 commit 把 150 个文件 `ruff format` 落地
- ruff format 0 欠债 (clean state)
- 不破坏 ruff check 0 / mypy 0 / 全量 1233+ passed
- 单独 commit, 不和 W36f/W36g 混, blame 干净

## 2. DoD 拆解 (对照 W33a 技术债清单 + W36f 收口基线)

- [ ] **D1** `ruff format --check src tests` 报 `150 files would be reformatted` 起点记录
- [ ] **D2** `ruff format src tests` 跑通, 无 error
- [ ] **D3** 落地后 `ruff format --check src tests` 报 `0 files would be reformatted, 185 files already formatted`
- [ ] **D4** `ruff check src tests` 仍 0 errors (格式化不引入新 lint 错)
- [ ] **D5** `mypy src/agent_swarm` 仍 0 errors (格式化不破坏类型)
- [ ] **D6** 全量 `pytest tests/unit tests/golden -q` 仍 1233+ passed (0 新失败)
- [ ] **D7** `git diff --stat` 报 150 files changed (确认 150 文件实际改)
- [ ] **D8** `tools/verify_w36e_dod.py` 5 项守门全过

## 3. 风险登记

| # | 风险 | 缓解 | 状态 |
|---|------|------|------|
| R1 | 格式化破坏字符串字面值 (e.g. SQL f-string) | ruff format 是 PEP 8 标准, 只调空白/引号/缩进, 不改字符串内容 | 🟢 标准 |
| R2 | 格式化引入新 lint 错 (D1 触发 S/E/W 规则) | D4 守门 ruff check 0, 不破则通过 | 🟢 守门 |
| R3 | 格式化破坏类型推断 (e.g. 多行表达式合并) | D5 守门 mypy 0, 不破则通过 | 🟢 守门 |
| R4 | 150 文件一次性 commit 污染 blame | 用 1 原子 commit, 标 "format only" 主题, git blame 可用 `git blame --ignore-revs-file` 排除 | 🟢 标准 |
| R5 | 格式化导致 1-2 个测试意外失败 (e.g. 字符串匹配) | D6 守门, 不破则回滚该文件单独排查 | 🟢 守门 |
| R6 | .venv / .gitignore 文件被误格式化 | ruff format 默认忽略 .gitignore + .venv 已在 .gitignore | 🟢 标准 |

## 4. 资源 / 预算

- **工时**: 30 分钟 (1 个 format 命令 + 守门 + commit)
- **关键路径**: D2 (format) → D3-D6 (5 守门) → D7 (diff stat) → D8 (verify 脚本) → commit
- **阻塞条件**: 无 (纯历史清理)
- **依赖**: 无 (ruff 已装, 沿用 W33a)

## 5. Check 守门点 (本轮 C 阶段必跑)

```bash
# 主守门
.venv/bin/python tools/verify_w36e_dod.py    # 5 项全过

# 标准
.venv/bin/ruff format --check src tests     # 0 files would be reformatted
.venv/bin/ruff check src tests              # 0 errors
.venv/bin/mypy src/agent_swarm              # Success
.venv/bin/pytest tests/unit tests/golden -q  # 1233+ passed (W36f baseline)

# 回归 (W36 阶段不破)
.venv/bin/pytest tests/unit/test_web_review_async.py -v  # W36f 18 case 不破
.venv/bin/pytest tests/golden/test_g029_*.py -v          # G-029 5 case 不破
```

## 6. Act 输出 (本轮 C 通过后必须落)

- [x] **D1** `ruff format --check` 起点 150 files ✅
- [x] **D2** `ruff format src tests` 跑通 ✅
- [x] **D3** 落地后 `ruff format --check` 报 185 files already formatted ✅
- [x] **D4** `ruff check src tests` 0 errors ✅
- [x] **D5** `mypy src/agent_swarm` 0 errors ✅
- [x] **D6** 全量 `pytest` 1238 passed (W36f baseline 1233 不破) ✅
- [x] **D7** `git diff --stat` 150 files changed ✅
- [x] **D8** `tools/verify_w36e_dod.py` 5/5 PASSED ✅

**W36e 闭环状态: ✅ Act 全部 8 项完成, 本轮 PDCA 闭环 (commit 见 git log, 本地不 push)**

## 7. 衔接 (W36e 完成后)

- **W36g** (release, 阻塞) — 0.5.0 final, 等 TestPyPI
- **W37** (LLM 真实接入) — OpenAI/Anthropic SDK + AdversarialVerifier 真实流程 (W36f 留口子)

## 8. 引用

- `PDCA.md` — 本轮循环模板
- `W36_RETRO_PLAN.md` §7 — W36e 候选 #2 (技术债, 并行)
- `W36f_PLAN.md` §7 — W36e 候选 (技术债, 推荐接 W36f 后)
- `docs/MEMORY.md` W33a 段 — "ruff format 136 欠债 (W33a 已知)"
- `tools/verify_w36f_dod.py` — W36f 8 项守门 (本 slice 复用模式, 简化为 5 项)
- `pyproject.toml` — ruff 配置
