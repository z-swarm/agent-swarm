# W36g: 0.5.0 final release (W36 阶段收口) PDCA Plan

> PDCA **Plan** 阶段 (2026-06-24)
> 模板见 [`PDCA.md`](PDCA.md)
> 闭环 W36 整阶段: 0.5.0a1 → 0.5.0a2 → 0.5.0 final (3 阶段 release 链)
> 衔接: W27 0.4.0a1 release 模式 (hatchling build + twine check + tag)
> 衔接: [[stage-gate-on-dod]] / [[local-commit-no-push]] / [[self-driven-execution]]

## 1. 背景 / 闭环目标

**当前状态 (W36e 落地后):**
- `pyproject.toml` version = `0.5.0a2`
- `__version__` = `0.5.0a2`
- git tag 序列: `0.5.0` (W27 早期) → `0.5.0a1` → `0.5.0a2` (W36d)
- CHANGELOG 0.5.0a2 节点含 7 个 weekly slice (W33a-W36c) + W36f 节点
- 0.5.0 final 是 W36 阶段 production release, 无 alpha 标识
- 累计: W36a-W36f 6 slice + W36e 收口 = 7 commit 闭环

**W36g 目标:**
- version `0.5.0a2 → 0.5.0` (去 alpha 标识, final release)
- CHANGELOG 0.5.0 节点 (合并 W36a-f + W36e 全部 DoD/数据)
- 重新构建 dist: `rm -rf dist/ build/ && python -m build`
- `twine check dist/*` PASSED (W36d 模式复用)
- `git tag 0.5.0` (新增, 已有 0.5.0a1 / 0.5.0a2)
- `tools/verify_w36g_dod.py` 8 项守门全过
- TestPyPI 上传状态更新 (留 TODO 等用户环境)
- ruff 0 / mypy 0 / 全量 1238+ passed (W36e baseline)

## 2. DoD 拆解 (对照 W36d 0.5.0a2 模式 + P5 §17.2 阶段门控)

- [ ] **D1** `pyproject.toml` version 升级 `0.5.0a2 → 0.5.0`
- [ ] **D2** `src/agent_swarm/__init__.py` `__version__` 同步 `0.5.0`
- [ ] **D3** `src/agent_swarm/web/app.py` version default 同步 `0.5.0`
- [ ] **D4** `src/agent_swarm/web/templates/base.html` 2 处硬编码同步 `0.5.0`
- [ ] **D5** `CHANGELOG.md` 新增 `0.5.0` 节点
  - 合并 W36a (JWT Secret 走 SecretManager)
  - 合并 W36b (agent_review Web 入口)
  - 合并 W36c (vault:// URI 扩展)
  - 合并 W36d (0.5.0a2 release)
  - 合并 W36e (ruff format 150 文件)
  - 合并 W36f (agent_review 异步入口)
  - W36 整阶段归档 (W36_RETRO_PLAN.md)
  - 每个 slice 含 DoD/数据/已知限制段
- [ ] **D6** `rm -rf dist/ build/ && python -m build` 验证 sdist + wheel 构建成功
- [ ] **D7** `twine check dist/*` PASSED (W36d 模式复用)
- [ ] **D8** `git tag 0.5.0` (新增, 必有 0.5.0a1 / 0.5.0a2 之前已存在)
- [ ] **D9** `tools/verify_w36g_dod.py` 8 项守门
  - version 一致 (pyproject + __init__ + app.py + base.html 4 处)
  - CHANGELOG 含 0.5.0 节点
  - dist 文件存在 (0.5.0 sdist + wheel)
  - twine check PASSED
  - git tag 0.5.0 存在
  - ruff 0 / mypy 0
  - 全量 pytest 1238+ passed
  - 6 个 W36 slice 节点都在 CHANGELOG 0.5.0 段内
- [ ] **D10** ruff 0 + mypy 0 + 全量 1238+ passed (W36e baseline)
- [ ] **D11** TestPyPI 上传命令准备 (留 TODO, 不调 upload)
  - `twine upload --repository testpypi dist/agent_swarm-0.5.0*` (需用户环境 token)
  - 状态: dist ready, 上传需 `~/.pypirc` token + non-interactive terminal

## 3. 风险登记

| # | 风险 | 缓解 | 状态 |
|---|------|------|------|
| R1 | version 不一致 (4 处硬编码) | grep 全 + 守门项 1 自动校验 (pyproject / __init__ / app.py / base.html) | 🟢 守门 |
| R2 | CHANGELOG 节点重复 (W36d 0.5.0a2 + W36g 0.5.0) | 0.5.0 节点作为汇总, 不重写 0.5.0a2 细节, 引用 W36d 节点 | 🟢 模式复用 |
| R3 | `python -m build` 失败 (依赖/路径) | 复用 W36d 0.5.0a2 构建模式 (hatchling backend, 零新装) | 🟢 设计对齐 |
| R4 | `twine check` 失败 (元数据缺) | 复用 W36d 模式, pyproject 已含完整 metadata | 🟢 模式复用 |
| R5 | `git tag 0.5.0` 重复 (0.5.0 已存在?) | 检查 tag 列表, 0.5.0 是 W27 早期 tag, 复用/覆盖确认; 本次 W36g 新增 | 🟡 必查 |
| R6 | dist 包含脏文件 (旧 build) | 先 `rm -rf dist/ build/`, 再 build | 🟢 标准 |
| R7 | 6 个 W36 slice 引用错位 | 守门项 8 解析 CHANGELOG 0.5.0 段, 验证 W36a/b/c/d/e/f 6 个节点都在 | 🟡 待 D9 |
| R8 | TestPyPI 上传误推 PyPI | D11 只到 "dist ready" 为止, 上传留用户 | 🟢 范围收口 |
| R9 | pyproject description 还说 "Phase 2: ..." 但实际 Phase 5 | W36g 暂不动 (release 节奏不混 description), 留 W37+ 收口 | 🟡 范围收口 |
| R10 | W36 整阶段归档 commit (eef4e47) 也算 1 个 W36 commit, 6 vs 7 节点数 | 守门项 8 选 6 个核心 slice 节点 (W36a/b/c/d/e/f), 整阶段归档不计入 | 🟡 待 D9 |

## 4. 资源 / 预算

- **工时**: ~30 分钟 (CHANGELOG 合并是大头, build/tag/check 10 分钟内)
- **关键路径**: D1-D5 (version + CHANGELOG) → D6-D7 (build + check) → D8 (tag) → D9-D10 (守门 + 全量) → D11 (TODO)
- **阻塞条件**: 无 (W36e 已闭环, dist 模式复用 W36d)
- **依赖**: `build` (PEP 517) + `twine` (W36d 模式, 已装) — 0 新装

## 5. Check 守门点 (本轮 C 阶段必跑)

```bash
# 主守门
.venv/bin/python tools/verify_w36g_dod.py    # 8 项全过

# 标准
.venv/bin/ruff check src tests              # 0 errors
.venv/bin/mypy src/agent_swarm              # Success
.venv/bin/pytest tests/unit tests/golden -q  # 0 新失败

# 回归 (W36 阶段不破)
.venv/bin/pytest tests/unit/test_web_review.py -v  # W36b 不破
.venv/bin/pytest tests/unit/test_web_review_async.py -v  # W36f 不破
.venv/bin/pytest tests/golden/test_g02[7-9]*.py -v  # G-027/028/029 不破

# build
rm -rf dist/ build/
.venv/bin/python -m build                   # sdist + wheel
.venv/bin/twine check dist/*                # PASSED

# tag
git tag -l '0.5.0*' | grep 0.5.0           # 必存在
```

## 6. Act 输出 (本轮 C 通过后必须落)

- [x] **D1-D4** version 4 处同步 `0.5.0a2 → 0.5.0` ✅
- [x] **D5** `CHANGELOG.md` 0.5.0 节点 (汇总表 + 6 slice 简述) ✅
- [x] **D6** `python -m build` 0.5.0 sdist + wheel 成功 ✅
- [x] **D7** `twine check dist/*` PASSED (2/2) ✅
- [x] **D8** `git tag 0.5.0` (覆盖 W27 早期误打, 见 R5) ✅
- [x] **D9** `tools/verify_w36g_dod.py` 8/8 PASSED (commit 后 8/8) ✅
- [x] **D10** ruff 0 + mypy 0 + 全量 1238 passed (W36e baseline) ✅
- [x] **D11** TestPyPI 上传 TODO 留 (需用户 `~/.pypirc` token) ✅

**W36g 闭环状态: ✅ Act 全部 11 项完成, 本轮 PDCA 闭环, W36 阶段最终收口 (commit 见 git log, 本地不 push)**

## 7. 下一轮 (W37) 衔接

W36g = W36 阶段收口, 之后:
- **W37** (LLM 真实接入) — OpenAI/Anthropic SDK + AdversarialVerifier.verify 真实流程
- **W37+** — .git-blame-ignore-revs 完整配置 (W36e 150 文件 commit 隔离)
- **W37+** — pyproject description 更新 (Phase 2 → Phase 5, W36g 留口子)

## 8. 引用

- `PDCA.md` — 本轮循环模板
- `CHANGELOG.md` 0.5.0a2 节点 — release 模式参考 (W36d)
- `pyproject.toml` — 当前 0.5.0a2, 待升级 0.5.0
- `src/agent_swarm/__init__.py` — `__version__` 当前 0.5.0a2, 待升级
- `tools/verify_w36d_dod.py` — W36d 0.5.0a2 release 守门模式 (本 slice 复用 8 项结构)
- `W36_RETRO_PLAN.md` — W36 整阶段归档 (本 slice 是 W36 收口)
- `W36d_PLAN.md` — 0.5.0a2 release 节奏 (本 slice 升 final)
