# W36d: 0.5.0a1 → 0.5.0a2 推进 (release) PDCA Plan

> PDCA **Plan** 阶段 (2026-06-24)
> 模板见 [`PDCA.md`](PDCA.md)
> 衔接: W36a/W36b/W36c 已闭环 + W27 0.4.0a1 release 模式
> 衔接: [[stage-gate-on-dod]] / [[local-commit-no-push]] / [[self-driven-execution]]

## 1. 背景 / 闭环目标

**当前状态:**
- `pyproject.toml` version = `0.5.0a1`
- `__version__` = `0.5.0a1`
- CHANGELOG 0.5.0a1 节点已就位 (P5 启动)
- W33a / W33b / W34 / W35 / W36a / W36b / W36c 7 个 weekly slice 已闭环
- 已本地 commit + push 到 origin (79c4067)

**W36d 目标:**
- 升级 version 0.5.0a1 → 0.5.0a2
- CHANGELOG 新增 0.5.0a2 节点, 合并 7 个 weekly slice 全部 DoD/数据/已知限制
- `python -m build` 验证 sdist + wheel
- `twine check dist/*` PASSED (W27 模式复用)
- `git tag 0.5.0a2` 标签
- 全量回归 + 守门 8 项
- 已知缺口 (TestPyPI 上传) 状态更新

## 2. DoD 拆解 (对照 W27 0.4.0a1 模式 + P5 §17.2)

- [ ] **D1** `pyproject.toml` version 升级 `0.5.0a1 → 0.5.0a2`
- [ ] **D2** `src/agent_swarm/__init__.py` `__version__ = "0.5.0a2"` 同步
- [ ] **D3** `CHANGELOG.md` 新增 `0.5.0a2` 节点
  - 合并 W33a (P0-1 防御深度)
  - 合并 W33b (WebState Postgres 持久化)
  - 合并 W34 (WebState JWT 鉴权)
  - 合并 W35 (WebState 跨进程 fan-out)
  - 合并 W36a (JWT Secret 走 SecretManager)
  - 合并 W36b (agent_review Web 入口)
  - 合并 W36c (vault:// URI 扩展)
  - 每个 slice 含 DoD/数据/已知限制段
- [ ] **D4** `python -m build` 验证 sdist + wheel 构建成功
- [ ] **D5** `twine check dist/*` PASSED (W27 模式)
- [ ] **D6** `git tag 0.5.0a2` 标签
- [ ] **D7** `tools/verify_w36d_dod.py` 8 项全过
  - version 一致 (pyproject + __init__)
  - CHANGELOG 含 0.5.0a2 节点
  - dist 文件存在 (sdist + wheel)
  - twine check PASSED
  - git tag 0.5.0a2 存在
  - ruff 0 / mypy 0
  - 全量 pytest 1204+ passed
  - 7 个 weekly slice 节点都在 CHANGELOG 0.5.0a2 内
- [ ] **D8** ruff 0 / mypy 0 / 全量 1204+ passed (W36c baseline)
- [ ] **D9** docs/MEMORY.md release 经验 + docs/P5-RETRO.md W36d 段
- [ ] **D10** 已知缺口状态更新
  - TestPyPI 上传 (需用户环境, 留 TODO)
  - DESIGN.md untrack (W23 已知, 状态保留)
  - 端到端 e2e (用户环境, 状态保留)

## 3. 风险登记

| # | 风险 | 缓解 | 状态 |
|---|------|------|------|
| R1 | CHANGELOG 节点重复 (W23-W35 节点在 0.5.0a1 内已写, W36 节点在 W36a-c 各自 commit 时已写) | 0.5.0a2 节点作为汇总 + 链接, 不重复写 detail, 引用各 W 节点 | 🟡 待 D3 设计 |
| R2 | version 不一致 (pyproject / __init__ / 散落的硬编码) | 守门第 1 项 + 跑 grep 检查所有硬编码 | 🟢 模式复用 |
| R3 | `python -m build` 失败 (依赖缺失 / 路径错) | 复用 W27 0.4.0a1 构建模式 (hatchling backend) | 🟢 设计对齐 |
| R4 | `twine check` 失败 (元数据缺) | 复用 W27 模式, pyproject 已含完整 metadata | 🟢 模式复用 |
| R5 | `git tag` 重复 (0.5.0a1 已存在?) | 查 tag, 如有 0.5.0a1 则先确认版本, 0.5.0a2 必新 | 🟢 检查 + 必新 |
| R6 | dist 包含脏文件 (旧 build / __pycache__) | 先 `rm -rf dist/ build/`, 再 build | 🟢 标准操作 |
| R7 | 7 个 weekly slice 节点引用错位 | 守门第 8 项: 解析 CHANGELOG 找 7 个 W## 节点 + 验证都在 0.5.0a2 段内 | 🟡 待 D7 实现 |
| R8 | TestPyPI 上传需求 → 误推 PyPI | D7 不调 twine upload, 只 check; 上传留用户 | 🟢 范围收口 |

## 4. 资源 / 预算

- **工时**: ~3 小时 (CHANGELOG 合并是大头, build/tag/check 10 分钟内)
- **关键路径**: D1-D3 (version + CHANGELOG) → D4-D5 (build + check) → D6 (tag) → D7-D8 (守门 + 全量) → D9-D10 (文档 + commit)
- **阻塞条件**: 无 (W36c 已闭环, 0.5.0a1 是 base)
- **依赖**: `build` (PEP 517) + `twine` (W27 模式) — 复用, 无新装

## 5. Check 守门点 (本轮 C 阶段必跑)

```bash
# 主守门
.venv/bin/python tools/verify_w36d_dod.py    # 8 项全过

# 标准
.venv/bin/ruff check src tests              # 0 errors
.venv/bin/mypy src/agent_swarm              # Success
.venv/bin/pytest tests/unit tests/golden -q  # 0 新失败

# 回归
.venv/bin/pytest tests/unit/test_web_jwt_*.py -v  # W36a-c 不破
.venv/bin/pytest tests/golden/test_g02*.py -v     # Golden 不破

# build
.venv/bin/python -m build                   # sdist + wheel
.venv/bin/twine check dist/*                # PASSED

# tag
git tag | grep 0.5.0a2                      # 必存在
```

## 6. Act 输出 (本轮 C 通过后必须落)

- [x] `pyproject.toml` version 升级 0.5.0a1 → 0.5.0a2
- [x] `src/agent_swarm/__init__.py` `__version__` 同步
- [x] `src/agent_swarm/web/app.py` + `base.html` 2 处硬编码同步
- [x] `CHANGELOG.md` 0.5.0a2 节点合并 (汇总表 + 7 slice 简述)
- [x] `dist/` sdist + wheel (本地构建产物, gitignored)
- [x] `git tag 0.5.0a2` (新增)
- [x] `tools/verify_w36d_dod.py` 守门脚本 — 8/8 PASSED
- [x] `docs/MEMORY.md` release 经验 (6 条)
- [x] `docs/P5-RETRO.md` W36d 段
- [x] 本地 commit (e7171a6) + push (含 tag 0.5.0a2)

**W36d 闭环状态: ✅ 全部 10 项 Act 输出完成,本轮 PDCA 已闭环**

## 7. 下一轮 (W36e) 预告

候选 (W36d 完成后):
- **W36e**: repo 级 `ruff format` 136 欠债 (历史清理)
- **W36f**: agent_review 全模式 (LLM + 对抗式) Web 异步入口
- **W36g**: 0.5.0 final (tested production release, 等用户环境 TestPyPI 验证后)

## 8. 引用

- `PDCA.md` — 本轮循环模板
- `CHANGELOG.md` 0.4.0a1 节点 — release 模式参考
- `pyproject.toml` — 当前 0.5.0a1, 待升级
- `src/agent_swarm/__init__.py` — `__version__` 当前 0.5.0a1, 待升级
- `tools/verify_p4_dod.py` — W27 0.4.0a1 release 守门模式
- W36a-c 节点: 全部已 CHANGELOG 写入, 0.5.0a2 节点汇总引用
