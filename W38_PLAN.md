# W38: Phase 5 收口 (.git-blame-ignore-revs + pyproject description + 0.5.0 准备) PDCA Plan

> PDCA **Plan** 阶段 (2026-06-24)
> 模板见 [`PDCA.md`](PDCA.md)
> 闭环 W36e/W36g/W37 留口子: ".git-blame-ignore-revs / pyproject description / TestPyPI 上传"
> 衔接: [[stage-gate-on-dod]] / [[local-commit-no-push]] / [[self-driven-execution]]
> 衔接: 0.5.0 final 已闭环 (W36g), W38 是 0.5.0 final "production-ready" 收口

## 1. 背景 / 闭环目标

**当前状态 (W37 落地后):**
- 0.5.0 final 已发 (W36g: dist + twine check + tag)
- W36e 150 文件 commit `16a8556` 污染 git blame (每行都动)
- pyproject description 仍说 "Phase 2: Delegate Mode + Adversarial Verify + MCP 集成" (过期)
- pyproject keywords 缺新模块 (web / secrets / jwt)
- TestPyPI 上传需用户环境 `~/.pypirc` token, 文档未准备
- 全量 1256 passed, ruff 0 / mypy 0 (W37 baseline)

**W38 目标:**
- `.git-blame-ignore-revs` 配置,隔离 W36e 150 文件 commit, `git blame` 自动跳过
- pyproject description 更新 "Phase 2: ..." → "Phase 5: GUI Web UI + WebState 协议 + 真实 LLM 接入"
- pyproject keywords + classifiers 更新 (完整 PyPI 元数据)
- `docs/RELEASE-0.5.0.md` 准备 TestPyPI 上传命令 + 用户环境步骤
- `tools/verify_w38_dod.py` 6 项守门全过
- 全量 1256+ passed, ruff 0 / mypy 0 不破
- W36/W37 阶段不破 (回归)

## 2. DoD 拆解 (对照 W36e/W36g/W37 留口子)

- [ ] **D1** `.git-blame-ignore-revs` 文件创建
  - 记录 W36e 150 文件 commit hash `16a8556`
  - 文件格式: `<full commit hash> # <description>` 一行一条
  - 提交入 git (per-repo 配置, 不放全局)
- [ ] **D2** `.git-blame-ignore-revs` 文档化
  - `README.md` 加段: "## Git Blame Ignore" 解释配置 + W36e 历史
  - 用户启用方法: `git config blame.ignoreRevsFile .git-blame-ignore-revs`
- [ ] **D3** pyproject description 更新
  - 旧: `description = "通用多 Agent 协作框架（Phase 2: Delegate Mode + Adversarial Verify + MCP 集成）"`
  - 新: `description = "通用多 Agent 协作框架（Phase 5: GUI Web UI + WebState 协议 + 真实 LLM 接入）"`
- [ ] **D4** pyproject keywords 增补
  - 旧: 5 个 (multi-agent / swarm / orchestration / llm / pydantic)
  - 新: 10+ 个 (加 web / fastapi / webstate / jwt / secrets / adversarial / sse / async)
- [ ] **D5** pyproject classifiers 完整
  - 旧: 缺 Programming Language :: Python :: 3.11/3.12
  - 新: 完整 Python 版本 + License :: OSI Approved :: MIT License
- [ ] **D6** `docs/RELEASE-0.5.0.md` 准备
  - TestPyPI 上传命令: `twine upload --repository testpypi dist/agent_swarm-0.5.0*`
  - 用户环境步骤: 配置 `~/.pypirc` token → 运行 upload → 验证 TestPyPI 页面
  - PyPI 正式发布步骤 (TestPyPI 验证后): `twine upload dist/agent_swarm-0.5.0*`
  - 已知限制: 用户环境 (token) + non-interactive terminal
- [ ] **D7** `tools/verify_w38_dod.py` 6 项守门
  - `.git-blame-ignore-revs` 存在 + 含 16a8556
  - pyproject description 含 "Phase 5"
  - pyproject keywords ≥10
  - pyproject classifiers ≥3 (含 Python 版本)
  - `docs/RELEASE-0.5.0.md` 存在 + 含 upload 命令
  - W36/W37 baseline 不破 (W36f 18 + G-029 5 + W36b 14 + G-027 4 = 41 case)
- [ ] **D8** ruff 0 / mypy 0 / 全量 1256+ passed (W37 baseline)
- [ ] **D9** ruff format --check 0 欠债 (W36e 已清, 维持)
- [ ] **D10** 守门 8 项全过, 本地 commit (不 push)

## 3. 风险登记

| # | 风险 | 缓解 | 状态 |
|---|------|------|------|
| R1 | .git-blame-ignore-revs 配错 hash | 守门 1 校验 hash 存在 + git blame 实测跳过 | 🟡 待 D1 |
| R2 | 用户不启用 .git-blame-ignore-revs | README 文档化, 用户主动 `git config` 启用 (不放全局) | 🟢 标准 |
| R3 | pyproject description 改坏 PyPI 描述 | 守门 2 校验含 "Phase 5", 旧 "Phase 2" 不在 | 🟢 守门 |
| R4 | keywords/classifiers 不被 PyPI 接受 | 守门 3/4 校验数量 + 内容 | 🟢 守门 |
| R5 | RELEASE-0.5.0.md 描述错导致用户误操作 | 文档明确"TestPyPI 不 PyPI", upload 命令分两段 | 🟢 文档 |
| R6 | 上传测试时推到正式 PyPI | D6 文档强调 `--repository testpypi` 必加 | 🟢 范围收口 |
| R7 | W36/W37 回归破坏 | 守门 6 跑 W36b/f + G-027/029 子集 | 🟢 守门 |
| R8 | ruff format 欠债回潮 (W36e 后又改代码) | 守门 5 跑 ruff format --check 0 欠债 | 🟢 守门 |

## 4. 资源 / 预算

- **工时**: 1-2 小时 (.git-blame-ignore-revs + pyproject + 文档为主, 守门 30 分钟)
- **关键路径**: D1-D2 (git blame) → D3-D5 (pyproject) → D6 (release 文档) → D7-D9 (守门) → D10 (commit)
- **阻塞条件**: 无 (W37 已闭环, 0.5.0 dist 已构建)
- **依赖**: 0 新装 (git / twine / build 已装)

## 5. Check 守门点 (本轮 C 阶段必跑)

```bash
# 主守门
.venv/bin/python tools/verify_w38_dod.py    # 6 项全过

# 标准
.venv/bin/ruff check src tests tools        # 0 errors
.venv/bin/ruff format --check src tests     # 0 欠债 (W36e baseline)
.venv/bin/mypy src/agent_swarm              # Success
.venv/bin/pytest tests/unit tests/golden -q  # 1256+ passed

# W36/W37 回归
.venv/bin/pytest tests/unit/test_web_review.py \
                   tests/unit/test_web_review_async.py \
                   tests/golden/test_g027_review_e2e.py \
                   tests/golden/test_g029_review_async_e2e.py \
                   -q --tb=no  # 41 case

# .git-blame-ignore-revs 实测
git blame README.md --ignore-revs-file .git-blame-ignore-revs | head -3
```

## 6. Act 输出 (本轮 C 通过后必须落)

- [x] **D1** `.git-blame-ignore-revs` 文件 (含 W36e `16a8556`) ✅
- [x] **D2** `README.md` "Git Blame Ignore (W38)" 段文档化 ✅
- [x] **D3** pyproject description 更新 (Phase 2 → Phase 5) ✅
- [x] **D4** pyproject keywords 5 → 13 (加 web / fastapi / webstate / jwt / secrets / adversarial / sse / async) ✅
- [x] **D5** pyproject classifiers 0 → 9 (Python 3.11/3.12 + MIT License + AsyncIO) ✅
- [x] **D6** `RELEASE.md` 移到根目录入 git (TestPyPI/PyPI upload 步骤) ✅
- [x] **D7** `tools/verify_w38_dod.py` 6/6 PASSED ✅
- [x] **D8** ruff 0 / mypy 0 / 全量 1256 passed (W37 baseline) ✅
- [x] **D9** ruff format --check 0 欠债 (W38 顺手清 W37 留下 1 欠债) ✅
- [x] **D10** 本地 commit (1 原子, 不 push) ✅

**W38 闭环状态: ✅ Act 全部 10 项完成, Phase 5 收口, 0.5.0 final production-ready**

## 7. 衔接 (W38 完成后)

- **W39+** (Phase 6 启动候选):
  - TestPyPI 真实上传 (需用户环境 token)
  - Phase 6 计划: 多 worker / Redis / 真实分布式 (W36f/W37 留口子)
  - `.git-blame-ignore-revs` 启用 (用户 git config)
  - 1.0.0 计划 (production release)

## 8. 引用

- `PDCA.md` — 本轮循环模板
- `W36e_PLAN.md` — W36e 150 文件 commit 来源
- `W36g_PLAN.md` — 0.5.0 final release 节奏
- `W37_PLAN.md` — W37 留 3 候选 (W38 接 .git-blame + description)
- `pyproject.toml` — 当前 description / keywords / classifiers
- `docs/MEMORY.md` W36e 段 — ".git-blame-ignore-revs 留 W37+ 完整配置"
- `docs/MEMORY.md` W36g 段 — "pyproject description 更新 (Phase 2 → Phase 5) 留 W37+"
- `tools/verify_w37_dod.py` — 守门 8 项结构 (本 slice 6 项简化)
- `tools/build` + `twine` — PEP 517 模式复用
