# agent-swarm Phase 5 计划（W28-W32, GUI Web UI）

> **起草日期**: 2026-06-23
> **起草者**: Mavis
> **依据**: `docs/PHASE4-RETRO.md`（W22-W27 收尾复盘）+ `DESIGN.md §15 / §16.2 #2 / §17.2` + `git log c0cb33d..HEAD`
> **基准 commit**: `c0cb33d`（CHANGELOG 0.5.0a1 + version bump）
> **当前状态**: W28 + W31 + W32 已交付；0.5.0a1 dist 已建；待 §17.2 DoD 校验（本文档落地后即可跑 `tools/verify_p5_dod.py`）

## 0. 拆分原则与 W29/W30 处理

对标 Phase 1（W1-W6 6 周垂直切片）/ Phase 3（W14-W21 9 周含 W14 拆 a/b）节奏：
- **每周一个垂直切片**，周末必须有可演示产出
- **每周末必须 `git tag w<N>-demo`**
- **当周 DoD 未通过 → 下周计划顺延**（§17.1 强制规则）
- **不允许"假桩"**

### W29/W30 为何空缺

W28 交付 GUI Web UI v1 后，原计划跟进：
- W29: WebState 后端持久化（SQLite/Redis 切换）
- W30: RBAC / auth v0（JWT 签发 + 路由级权限）

**实际决策**：W29/W30 合并下沉到 W31，**不开独立切片**。理由：
1. W28 收尾（`f7c3cb6`）后,git log 直接跳到 W31 CLI 集成（`3150a73`）,中间无 commit 表明 **W29/W30 实质未启动**
2. W31 切片的"把 Web UI 接到 swarm run CLI"是更紧迫的端到端闭环（没有 CLI 入口,W28 的 Web UI 只能手工 uvicorn 拉起,违反 §17.1 垂直切片原则）
3. W29/W30 的范围（持久化 / 鉴权）属于**生产化纵深**,与"垂直切片增加新维度"节奏不符,移到 W33+ 候选更合适
4. P5 §17.2 已显式标注 "W29-W30 合并入 W31"——阶段门控可追溯

## 1. Phase 5 整体节奏（5 周, 3 切片 + 2 合并）

| 周次 | 演示目标（DoD） | 新增维度 | 沿用 |
|------|----------------|---------|------|
| **W28** | `pip install -e ".[web]" && uvicorn agent_swarm.web:app` 启动 4 页面 + WS 实时事件流 | FastAPI app + HTMX 模板 + WebSocket + WebState 缓冲 | — |
| **W31** | `agent-swarm run examples/w31_web_with_swarm.yaml --web` 启动 swarm 同时拉起 Web UI, 事件双向打通 | WebStateSink + CLI `--web` 选项 + uvicorn 集成 + 干净关闭 | W28 |
| **W32** | `agent-swarm run --web --web-worktree-repo <path>` 闭环 WorktreeManager ↔ Web UI worktrees 页面 | `create_app(worktree_manager=)` + CLI `--web-worktree-*` | W28 + W31 + P4-W22 WorktreeManager |

| **W29** | **合并入 W31**（WebState 后端持久化下沉到 W33+） | — | — |
| **W30** | **合并入 W31**（RBAC/auth v0 下沉到 W33+） | — | — |

## 2. P5 候选（W33+ 待启）

来自 `docs/PHASE4-RETRO.md` 待启动清单 + W29/W30 下沉项 + P5 §17.2 缺口：

| 优先级 | 候选 | 工作量 | 价值 | 备注 |
|-------|------|-------|------|------|
| **高** | **W33 WebState 持久化**（Postgres,W29 下沉） | 1-2 周 | Web UI 重启不丢事件 | **决策锁定**:Postgres(与 W25 后端一致) |
| **高** | **W34 RBAC / auth v0**（JWT,W30 下沉） | 2-3 周 | 多用户生产部署前置 | **决策锁定**:JWT(无状态,与 MCP source 分级配套) |
| **中** | **W35 Web UI v2**（agents/tasks 页面交互：从只读到操作） | 2 周 | 真正"可操作"而非只读仪表盘 | HTMX 已铺好,增量 |
| **中** | **G-022 Golden Case**（Web UI 端到端：swarm → WS 推送 → 浏览器可见） | 1 周 | 把 P5 锁进 CI 守门 | ✅ **已落地 (0.5.0 周期)**:6 cases 全过 |
| **低** | **多语言 SDK**（Go/TypeScript,§16.2 #5 倾向 Python 优先） | 4 周 | 跨语言生态 | 用户决定再启 |
| **低** | **分布式 swarm**（§15 远期） | 8 周 | 跨机器调度 | 远期 |

## 3. P5 DoD 守门链

| 工具 | 角色 |
|------|------|
| `tools/verify_p5_dod.py` | 阶段级守门（W28/W31/W32 三切片 + Phase 5 整体） |
| `tools/verify_p4_dod.py` | 历史回归（确保 P5 不破坏 P4） |
| `tools/verify_p3_dod.py` | 历史回归（P3） |
| `pytest tests/unit/test_web.py` | W28 单测 ≥29 |
| `pytest tests/unit/test_web_state_sink.py` | W31 单测 ≥10 |
| `pytest -q` | 全量回归（0 failed） |

## 4. 已知风险与缓解

| 风险 | 缓解 | 状态 |
|------|------|-----|
| `fastapi`/`uvicorn`/`jinja2` 是 `[web]` extras, 默认安装缺失 | W28 §17.2 DoD ① 显式要求 `pip install -e ".[web]"`;README 启动方式同步 | ✅ 文档化 |
| `WebState` 事件缓冲 500 条上限, 高负载会丢老 | W33 持久化解决;短期 UI 加 "older events truncated" 提示 | 🟡 已知限制,文档化 |
| 无 RBAC,单用户本地信任 | W34 解决;短期 `bind 127.0.0.1` 默认防外网 | ✅ 已有 (W31 `--web-host` 默认 127.0.0.1) |
| Worktree 页面需 `app.state.worktree_manager` 注入,缺则空 | W32 显式注入 CLI 路径;`getattr` 路由兜底 | ✅ 已落地 (W32) |
| `demos/wk28/31/32-*.mp4` 录屏缺失（§17.1 三件套） | 需带 GUI 环境手工录制;`demos/README.md` 已写占位 | 🟡 已知缺口,等 GUI 环境 |

## 5. 发布清单（0.5.0a1）

- [x] W28 + W31 + W32 commits
- [x] CHANGELOG 0.5.0a1 节点
- [x] pyproject.toml version = "0.5.0a1"
- [x] 0.5.0a1 sdist + wheel 构建（`dist/`）
- [x] git tag 0.5.0 / 0.5.0a1
- [ ] **DESIGN §17.2 P5 DoD 补全**（本文档同步落地后即完）
- [ ] **`tools/verify_p5_dod.py` 落地**（与本文档同步）
- [ ] **`docs/PHASE5-PLAN.md` 落地**（本文档）
- [ ] **`.gitattributes` + core.autocrlf 修 CRLF 噪声**（146 文件 0 改动污染）
- [ ] ruff 0 / mypy 0 / 全量 0 failed（**已知**:`fastapi` 缺装,需 `pip install -e ".[web]"` 再跑 verify_p5_dod.py）
- [x] **G-022 Golden Case** (6 cases: sink 推送 / WS 端到端 / partials HTML / 多订阅 fan-out / 缓冲丢老 / 异常隔离)
- [ ] **`git push origin main` + `git push origin 0.5.0a1`**（等 SSH/PyPI token）
- [ ] **TestPyPI 发 0.5.0a1**（**已尝试**, 缺 token + non-interactive terminal 阻塞, 见下)
- [ ] **PyPI 正式发 0.5.0**（TestPyPI 验证后）

### TestPyPI 发版命令(用户执行)

```bash
# 1) 准备 token: 登录 https://test.pypi.org/manage/account/token/ 生成
#    存到 ~/.pypirc 或环境变量
cat > ~/.pypirc <<'EOF'
[testpypi]
username = __token__
password = pypi-AgEIcHlwaS5vcmcC...   # 你的 token

[pypi]
username = __token__
password = pypi-AgEIcHlwaS5vcmcC...   # 正式 token
EOF
chmod 600 ~/.pypirc

# 2) 发到 TestPyPI
.venv/bin/twine upload --repository testpypi dist/agent_swarm-0.5.0a1*

# 3) 验证可装
.venv/bin/pip install -i https://test.pypi.org/simple/ agent-swarm==0.5.0a1

# 4) 跑一次 verify 确认 0.5.0a1 wheel 可用
.venv/bin/python -c "import agent_swarm; from agent_swarm.web import create_app; print('OK')"
.venv/bin/python tools/verify_p5_dod.py

# 5) 2 周 CI 跑稳后切 0.5.0 stable
#    pyproject.toml version 改 0.5.0 + rebuild + twine upload (default = PyPI)
```

## 6. 决策锁定（2026-06-23 用户确认）

| # | 决策 | 选项 | 选定 | 理由 |
|---|------|------|------|------|
| 1 | **W33 持久化后端** | SQLite / Redis / **Postgres** | **Postgres** | 与 W25 `PostgresBackend` 复用同 asyncpg 池;生产场景 QPS / 持久性 / schema 演进均优;WebState 单表 `events(seq PK, ts, type, payload JSONB)` |
| 2 | **W34 鉴权模式** | JWT(无状态) / Session+Cookie(有状态) | **JWT** | 与 MCP source 分级(W20)+ 飞书 HMAC 风格一致;前端 HTMX 无 CSRF 暴露面;`Authorization: Bearer` 头即可,多端复用 |
| 3 | **G-022 进 0.5.0** | 是 / 推迟到 W36 | **是** | G-018/019/020 都进 0.3.0,P5 同样应有 E2E 守门;1 天工作量可承担 |
| 4 | **PyPI 发版策略** | 0.5.0a1 → 0.5.0 / 直接 0.5.0 | **0.5.0a1 先 TestPyPI** | alpha 跑 2 周 CI 反馈,稳定后切 0.5.0 stable(沿用 P3/P4 节奏) |

## 7. 引用

- `CHANGELOG.md` — 0.5.0a1 完整变更日志
- `DESIGN.md` §15 / §16.2 #2 / §17.2 — P5 计划 + DoD
- `docs/PHASE4-RETRO.md` — P4 复盘（候选来源）
- `docs/PHASE3-PLAN-2026-06-20.md` — 计划文档模板
- `src/agent_swarm/web/` — W28 交付
- `src/agent_swarm/observability/web_state_sink.py` — W31 交付
- `examples/w28_web_demo.yaml` / `w31_web_with_swarm.yaml` / `w32_web_with_worktree.yaml` — 演示入口
- `tools/verify_w33_dod.py` — W33 收尾后写(Postgres 持久化守门)
- `tools/verify_w34_dod.py` — W34 收尾后写(JWT 鉴权守门)
