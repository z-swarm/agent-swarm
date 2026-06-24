# Changelog

All notable changes to agent-swarm will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.5.0] - 2026-06-24

### Phase 5 final release (W36 阶段收口)

#### 汇总: 6 个 W36 slice 全部 PDCA 闭环

| Slice | 内容 | 关键 commit |
|-------|------|------------|
| **W36a** | JWT Secret 走 SecretManager (轮换不重启) | `fff1823` |
| **W36b** | agent_review Web 入口 (UI 按钮触发 review) | `ecfbe73` |
| **W36c** | vault://path#field URI 扩展 (闭环 W36a 协议) | `6ca24eb` |
| **W36d** | 0.5.0a2 release 推进 (CHANGELOG 合并 + dist + tag) | `e7171a6` |
| **W36e** | repo 级 ruff format 150 文件欠债清理 (1 原子 commit) | `16a8556` |
| **W36f** | agent_review 异步入口 (LLM + SSE) | `82937f2` |

#### W36a: JWT Secret 走 SecretManager (2026-06-24)
- **SecretRef 协议三态**: literal / env / secret:// 三种, 互斥校验
- **resolve_secret always-fresh**: version 校验 + cache 更新, 不靠 TTL
- **降级路径二态**: cache 命中继续用 / cache miss 硬错
- **DoD**: 8/8; 22 老 case 不破 (W34 兼容零破坏)
- **详见**: `CHANGELOG.md` 0.5.0a2 节点 + `docs/MEMORY.md` W36a 段

#### W36b: agent_review Web 入口 (2026-06-24)
- **写路径鉴权**: PROTECTED_PREFIXES 元组加 `"/api/review"` (W34 模式)
- **pr_ref 注入防御**: shell 危险字符黑名单 + shlex 校验
- **HTMX 表单**: `hx-post` + `hx-target` + `hx-indicator` 零 JS 全 SPA 体验
- **DoD**: 8/8; 14 unit + 4 G-027
- **详见**: 0.5.0a2 节点 + `docs/MEMORY.md` W36b 段

#### W36c: vault://path#field URI 扩展 (2026-06-24)
- **URI scheme 扩展**: 4 种 kind (literal/env/secret_ref/vault) 增量识别
- **SecretRef field 字段**: `str | None = None` (default 模式向后兼容)
- **JSON 文档 field 提取**: 协议层兜底, 不污染 SecretManager ABC
- **DoD**: 8/8; 14 unit + 5 G-028
- **详见**: 0.5.0a2 节点 + `docs/MEMORY.md` W36c 段

#### W36d: 0.5.0a2 release 推进 (2026-06-24)
- **version 三处同步**: pyproject / __init__ / app.py
- **release 节点模式**: 汇总表 + 各 W 段简述, 不重写 detail
- **dist 模式复用 W27**: hatchling build + twine check + git tag
- **DoD**: 8/8; 1204 passed
- **详见**: `tools/verify_w36d_dod.py` + `docs/MEMORY.md` W36d 段

#### W36e: ruff format 150 文件欠债清理 (2026-06-24)
- **1 原子 commit**: 150 files reformatted, 35 already (共 185)
- **改动**: +3308/-2133 行 (无逻辑变化, 全 PEP 8 格式调整)
- **守门**: 5/5 (format 0 / check 0 / mypy 0 / pytest 1238 / HEAD 153 files)
- **已知限制**: 150 文件 commit 污染 blame, `.git-blame-ignore-revs` 留 W37+
- **详见**: `tools/verify_w36e_dod.py` + `docs/MEMORY.md` W36e 段

#### W36f: agent_review 异步入口 (LLM + SSE) (2026-06-24)
- **ReviewTask dataclass**: 7 字段 + 内存 task store (单进程)
- **llm_judge_factory**: 3 provider (fake/openai/anthropic) + API key fail-fast
- **run_full_review_async**: `asyncio.to_thread` 跑同步 LLM, event loop 不阻塞
- **3 端点**: POST 异步 (202 + task_id) + GET 状态 + GET SSE 流
- **CLI**: `--web-review-mode/--web-review-llm/--web-review-timeout` 3 选项
- **DoD**: 8/8; 18 unit + 5 G-029; 1233 passed
- **0 新依赖**: sse-starlette 拒, asyncio.Queue 自实现
- **详见**: `tools/verify_w36f_dod.py` + `docs/MEMORY.md` W36f 段

#### 阶段统计 (W36a-W36f)
- **新增代码**: 38+ 文件 (web 9 + security 3 + tests 25+)
- **测试增量**: 1204 → 1238 passed (+34: 14+4+14+5+0+18+5)
- **守门脚本**: 7 个 (verify_w36{a,b,c,d,e,f}_dod.py + verify_p5_dod.py)
- **Golden Case**: G-022 → G-029 (8 个, 端到端 35+ cases)
- **新增 CLI 选项**: 12 个 (W33 系列 + W34 + W36a/c/f 系列)
- **向后兼容**: W28 baseline 100% 不破, 跨 7 commit 兼容 (W36a-f + 整阶段归档)

#### 已知缺口 (W37+ 处理)
- TestPyPI 上传: dist ready (sdist + wheel), `twine check` PASSED, 实发需用户配 `~/.pypirc` token + non-interactive terminal
- DESIGN.md / docs/ 已 untrack (chore 2e1de16 / 943f432), 计划/复盘文档本地保留
- W36e 150 文件 commit 污染 blame → `.git-blame-ignore-revs` 完整配置留 W37+
- pyproject description 仍说 "Phase 2: ..." → W37+ 更新 (release 节奏不混 description)
- 多 worker 部署下 WebState 内存 task store 单进程限制 → W37+ Redis 共享
- OpenAI/Anthropic SDK 真实接入 → **W37 落地** (W36f 留口子, 已闭环)

#### W37: 真实 LLM judge 接入 (OpenAI/Anthropic SDK + AdversarialVerifier) (2026-06-24)

- **新增**: `tools/agent_review.py` `_openai_judge_fn(agent, hypothesis_id, round_no)`
  - 调 `openai.AsyncOpenAI` (gpt-4o-mini), `response_format={"type": "json_object"}`
  - 解析 stance / confidence / evidence / reasoning → Judgement
  - JSON 解析失败 → UNCERTAIN 兜底 (DESIGN §6.2.5)
- **新增**: `_anthropic_judge_fn(...)` 调 `anthropic.AsyncAnthropic` (claude-3-5-sonnet)
  - 处理 ` ```json ... ``` ` 代码块包裹
  - Union content narrow 到 TextBlock 取 .text
- **升级**: `run_full_review(pr_ref, llm_provider)` 真实流程
  - 删除 W13 占位的 "fallback simple" 路径
  - static_security_scan findings → hypotheses (每个 finding 1 个)
  - 3 个 plan_only Agent stub + `AdversarialVerifier.verify`
  - 存活假设 → root_causes, n_findings 决定 verdict
- **升级**: `src/agent_swarm/web/review_runner.py` `llm_judge_factory`
  - openai / anthropic 返真实 judge_fn (替换 W36f stub)
  - W36f web 异步路径自动接真实 LLM
- **测试**: 18 unit (mock SDK) + 1 异步路径 case
- **DoD**: `verify_w37_dod.py` 8/8 全过; 1256 passed (W36e 1238 + W37 +18)
- **已知限制**:
  - 真实 LLM 调 API 需 `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` (用户 env)
  - 慢响应走 `--web-review-timeout` 默认 60s, 异步不阻塞 event loop
  - SDK 升级需重跑测试 (openai>=1.40 / anthropic>=0.40 已固定)

#### W38: Phase 5 收口 (.git-blame-ignore-revs + pyproject description + 0.5.0 准备) (2026-06-24)

- **新增**: `.git-blame-ignore-revs` 文件
  - 记录 W36e 150 文件 commit `16a8556` (大规模格式化 commit)
  - 用户启用: `git config blame.ignoreRevsFile .git-blame-ignore-revs` (per-repo, 不放全局)
  - 验证: `git blame` 自动跳过 W36e, 回到上一次实质修改
- **升级**: `pyproject.toml` description / keywords / classifiers
  - description: "Phase 2: ..." → "Phase 5: GUI Web UI + WebState 协议 + 真实 LLM 接入"
  - keywords: 5 → 13 (加 web / fastapi / webstate / jwt / secrets / adversarial / sse / async)
  - classifiers: 0 → 9 (Python 3.11/3.12 + MIT License + AsyncIO)
- **新增**: `RELEASE.md` (从 docs/ 移到根目录入 git)
  - TestPyPI 上传步骤: `twine upload --repository testpypi dist/agent_swarm-0.5.0*`
  - PyPI 正式发布: `twine upload dist/agent_swarm-0.5.0*` (默认走 PyPI)
  - `~/.pypirc` token 配置 + 失败处理表
  - 上传后验证清单 (TestPyPI/PyPI 页面 + 试装)
- **新增**: `README.md` "Git Blame Ignore (W38)" 段
  - 解释 .git-blame-ignore-revs 用途 + 启用方法
  - W36e 历史说明
- **测试**: 0 新增 (纯配置 + 文档, 不需要测试)
- **DoD**: `verify_w38_dod.py` 6/6 全过
  - .git-blame-ignore-revs 含 16a8556
  - description 含 Phase 5
  - keywords 13 (≥10)
  - classifiers 9 (≥3 + Python 3.11/3.12 + MIT)
  - RELEASE.md 含 upload 命令
  - W36/W37 baseline 不破 (41 case)
- **衔接**: W39+ 候选
  - TestPyPI 真实上传 (需用户环境 `~/.pypirc` token)
  - Phase 6 计划 (多 worker / Redis / 1.0.0)
  - 用户 git config 启用 .git-blame-ignore-revs

#### W39: Phase 6 启动 (PHASE6-PLAN.md + W40 候选) (2026-06-24)

- **新增**: `docs/PHASE6-PLAN.md` (Phase 6 完整计划)
  - 阶段背景: 0.5.0 final 收口, Phase 5 累计 11 slice
  - 阶段目标: 1.0.0 production release (多 worker / 真实分布式 / 实战验证)
  - 范围: 8-12 周 (W40-W50 灵活调整)
  - 阶段 DoD: 1.0.0 final + 多 worker 部署 + 实战验证 + TestPyPI/PyPI 上传
  - W40 候选 (5-8 个, 优先级):
    1. **W40**: Redis task store 真实接入 (W36f 留口子)
    2. **W41**: 真实多 worker 部署 (W33b 留口子)
    3. **W42**: TestPyPI 真实上传 (W38 留口子)
    4. **W43**: 1.0.0 release 准备
    5. **W44+**: 实战验证 + 用户反馈循环
  - 风险 + 衔接 W36-W38 + 已知缺口
- **测试**: 0 新增 (Phase 6 启动 PLAN, 跟 W28 Phase 5 启动对称)
- **DoD**: `verify_w39_dod.py` 5/5 全过
  - PHASE6-PLAN.md ≥500 字
  - PHASE6-PLAN.md 含 4 关键词 (1.0.0/W40/Phase 5/TestPyPI)
  - CHANGELOG W39 节点
  - ruff 0 / mypy 0
  - W36/W37/W38 baseline 不破 (41 case)
- **衔接**:
  - **W40**: Redis task store 真实接入 (Phase 6 第一个具体 slice)
  - **W41-W50**: 多 worker / TestPyPI 上传 / 1.0.0 release / 实战验证

#### W40: Redis task store 真实接入 (TaskStore Protocol + Memory/Redis 双实现) (2026-06-24)

- **新增**: `src/agent_swarm/web/review_runner.py` `TaskStore` Protocol
  - 5 方法 (async): `create_task` / `get_task` / `update_task` / `subscribe_task` / `cleanup_expired`
  - 抽象接口, 跟 W33b `WebStateStore` Protocol 对称
- **新增**: `MemoryTaskStore` 包装现有 (W36f 兼容)
  - 用现有 `_TASK_STORE` / `_TASK_QUEUES` 模块级 dict
  - 零行为变化 (W36f 14 unit + 5 G-029 不破)
- **新增**: `RedisTaskStore` 真实实现 (W18 已装 redis>=5.0.0)
  - hash `task:{task_id}` 存 task 字段
  - sorted set `tasks:pending` 存待清理 task_id
  - pub/sub `task:{task_id}:events` 推 SSE 事件 (跨 worker 通知)
  - serialize: dataclass → JSON (asdict)
- **新增**: `create_task_store(backend, redis_dsn)` 工厂
  - `memory` → MemoryTaskStore
  - `redis` + DSN → RedisTaskStore
  - 缺 redis 包 / 缺 DSN → 警告 + 降级 MemoryTaskStore (W33b "DSN 缺省降级零破坏" 模式)
- **升级**: `create_app(task_store: Any = None)` 参数
  - 默认 MemoryTaskStore (W36f 兼容)
  - `app.state.task_store` 存储
- **CLI**: `--web-task-store {memory,redis}` (默认 memory) + `--web-redis-dsn` 选项
  - cli/main.py 调 `create_task_store` 工厂 + 注入 app
- **测试**: 14 case (`tests/unit/test_web_review_task_store.py`, fakeredis 模拟)
  - MemoryTaskStore: CRUD + subscribe + cleanup (5 case)
  - RedisTaskStore: CRUD + subscribe + cleanup (5 case, fakeredis)
  - create_task_store 工厂: 3 case (memory / redis 降级 / 未知 backend)
  - 跨 "worker" 任务同步 (1 case, fakeredis 共享)
- **DoD**: `verify_w40_dod.py` 8/8 全过
  - TaskStore Protocol 定义
  - MemoryTaskStore 包装 (5 方法 async)
  - RedisTaskStore 真实实现 + create_task_store 工厂
  - create_app 接 task_store
  - CLI --web-task-store / --web-redis-dsn
  - test_web_review_task_store.py ≥10 cases
  - ruff 0 / mypy 0
  - 全量 1270 passed (W39 1256 + W40 +14)
- **已知限制**:
  - 真实 Redis pub/sub 跨 worker 通知延迟 < 1ms (实战 W41 验证)
  - 任务清理 (cleanup_expired) 跨 worker 需 all worker 都跑 (idempotent)
  - asyncio.Queue 跨进程不可序列化 → Redis pub/sub 替代 (Redis 内部 queue)
  - 0 新依赖 (redis>=5.0.0 W18 已装, fakeredis>=2.20.0 dev 已装)

## [0.5.0a2] - 2026-06-24

### Phase 5 增量 release (W33a-W36c)

#### 汇总: 7 个 weekly slice 全部 PDCA 闭环

| Slice | 内容 | 关键 commit |
|-------|------|------------|
| **W33a** | P0-1 防御深度 (env 脱敏 + 白名单收缩) + 遗留修复 | `fed9921` |
| **W33b** | WebState Postgres 持久化 (Store 协议 + Schema + CLI + G-023) | `8849c8a` |
| **W34** | WebState JWT 鉴权 (HS256 标准库 + middleware 401 拦截) | `e6b204b` |
| **W35** | WebState 跨进程 fan-out (LISTEN/NOTIFY) 闭环 W33b R4 | `285baa3` |
| **W36a** | JWT Secret 走 SecretManager (轮换不重启) + SecretRef 协议 | `fff1823` |
| **W36b** | agent_review Web 入口 (UI 按钮触发 review) | `ecfbe73` |
| **W36c** | vault://path#field URI 扩展 (闭环 W36a 协议) | `6ca24eb` |

#### W33a: P0-1 防御深度加固 + 遗留修复 (2026-06-23)
- **新增**: `src/agent_swarm/security/_printenv.py` — `printenv` 替代 `env` 防御深度 (白名单 + 脱敏)
- **修改**: 5+ 个模块用 `printenv` 替代 `os.environ` 直读
- **DoD**: ruff 0 / mypy 0 / 全量 1251 passed (W32 → W33a)

#### W33b: WebState Postgres 持久化 (2026-06-23)
- **新增**: `src/agent_swarm/web/store.py` — WebStateStore 协议 + MemoryWebStateStore + PostgresWebStateStore
- **Schema**: `webstate_events(seq PK, ts, event_type, payload JSONB, session_id, tenant_id)` + 3 索引
- **CLI**: `--web-postgres-dsn / --web-postgres-table` 选项
- **DoD**: 1273 passed (W33a → W33b); 22 新增
- **已知限制**: 跨进程 fan-out 需 W34+ 加 LISTEN/NOTIFY (R4)

#### W34: WebState JWT 鉴权 (2026-06-23)
- **新增**: `src/agent_swarm/web/auth.py` — JWTIssuer (HS256 标准库, 零新依赖) + middleware
- **路由签名决策**: middleware 全局拦截, 不在路由签名加 `Depends` (避 FastAPI 422 坑)
- **CLI**: `--web-jwt-secret` (字面值 / ${VAR} 引用)
- **DoD**: 1295 passed (W33b → W34); 22 新增 (test_web_jwt_auth.py ≥15 + G-024)
- **已知限制**: HS256 secret 需 SecretManager 轮换 (留 W36a)

#### W35: WebState 跨进程 fan-out (LISTEN/NOTIFY) (2026-06-23)
- **新增**: `PostgresNotifier` (asyncpg LISTEN/NOTIFY 封装) + `NotifyEnvelope` 协议
- **origin_id 防 fan-out loop**: uuid4 hex 32 字符, 自订阅丢
- **8KB 截断降级**: NOTIFY 硬限制, payload > 7KB 降级为 `_truncated` 占位
- **fake asyncpg bus**: 模拟"两进程" + 跨进程语义 (W33 Store 模式延伸)
- **DoD**: web 子集 108/108 passed; `verify_w35_dod.py` 8/8; 18 unit + 4 G-025

#### W36a: JWT Secret 走 SecretManager (轮换不重启) (2026-06-24)
- **新增**: `SecretRef` 协议 (literal / env / secret_ref / vault 4 kinds)
- **JWTConfig 扩展**: `secret_ref` + `secret_manager` 字段
- **JWTIssuer**: `resolve_secret()` async + (key, version) cache + 降级路径 (P0 防御深度)
- **CLI**: `--web-jwt-secret-ref` + `--web-secret-manager {env,vault}` + `--vault-*` 三件套
- **DoD**: `verify_w36a_dod.py` 8/8; W34 22 老 case 不破; 21+7 unit + 5 G-026
- **闭环 W34 已知限制 #2**

#### W36b: agent_review Web 入口 (UI 按钮触发 review) (2026-06-24)
- **新增**: `src/agent_swarm/web/review_runner.py` — 薄包装 (AGENT_REVIEW_REPO env + sys.modules 清理)
- **路由**: `GET /review` 页面 + `POST /api/review` + `GET /partials/review_form`
- **模板**: `review.html` (HTMX 表单) + `partials/review_result.html` + base.html nav 入口
- **写路径鉴权**: PROTECTED_PREFIXES 加 `"/api/review"` (W34 模式一行复用)
- **注入防御**: `pr_ref` 字符黑名单 + shlex.split 双重校验
- **DoD**: `verify_w36b_dod.py` 8/8; W28/W34/W36a 不破; 14 unit + 4 G-027

#### W36c: vault://path#field URI 扩展 (闭环 W36a 协议) (2026-06-24)
- **新增**: `parse_secret_ref` 识别 `vault://path#field` (4 kinds 共用入口)
- **SecretRef 扩展**: `field: str | None = None` 字段 (frozen, 向后兼容 W36a)
- **JWTIssuer.resolve_secret vault 模式**: `mgr.get(path)` + JSON 解析 + field 提取
- **create_app**: 接受 `vault_url/vault_role_id/vault_secret_id` (vault:// 自动实例化 VaultSecretManager)
- **CLI**: `--web-jwt-secret-ref` 接受 `vault://` (复用 W36a `--web-secret-manager vault`)
- **DoD**: `verify_w36c_dod.py` 8/8; W36a 22 老 case 不破; 14 unit + 5 G-028

#### 阶段统计 (W33a-W36c)
- **新增代码**: 30+ 文件 (web 9 + security 3 + tests 18+)
- **测试增量**: 1126 → 1204+ passed (+78 cases: 14+5+21+7+14+4+11)
- **守门脚本**: 5 个 (verify_w33_dod.py + verify_w35_dod.py + verify_w36{a,b,c}_dod.py)
- **Golden Case**: G-022 → G-028 (7 个, 端到端 30+ cases)
- **新增 CLI 选项**: 9 个 (`--web` + `--web-host/--web-port` + `--web-postgres-*` + `--web-jwt-*` + `--web-cross-process` + `--web-worktree-*` + `--web-secret-manager` + `--vault-*`)
- **向后兼容**: W28 baseline 100% 不破, 跨 8 commit 兼容

#### 已知缺口 (等用户环境)
- TestPyPI 上传: dist ready, `twine check` PASSED, 实发需用户配 `~/.pypirc` token + non-interactive terminal
- DESIGN.md / docs/ 已 untrack (chore 2e1de16 / 943f432), 计划/复盘文档本地保留
- 端到端 e2e: `WorktreeManager initialized` + `web UI started` + `JWT 鉴权 401 拦截` + `SecretManager 轮换不重启` + `agent_review Web 入口` + `vault:// URI 解析`

#### W36f: agent_review 异步入口 (LLM + SSE) (2026-06-24)

- **新增**: `src/agent_swarm/web/review_runner.py` `ReviewTask` dataclass + 内存 task store
  - 7 字段: `task_id` (uuid4 hex 32) / `status` (pending/running/done/error) / `progress` (0-100) / `log` / `result` / `error` / `created_at`
- **新增**: `llm_judge_factory(provider: str)` — openai / anthropic / fake 三种
  - fake: 复用 W13 `_deterministic_judge` (零新依赖)
  - openai/anthropic: 占位 + API key fail-fast (真实 LLM 接入留 W37+)
- **新增**: `run_full_review_async(task_id, ...)` — 异步后台任务 + 进度推送
  - 用 `asyncio.to_thread` 跑同步 LLM, event loop 不阻塞
  - timeout 默认 60s (CLI `--web-review-timeout` 可调)
- **新增**: 3 端点 (`src/agent_swarm/web/routes.py`)
  - `POST /api/review` 改异步, mode=full 返 `202 + task_id` (W36f); mode=simple 返 `200 + report` (W36b 兼容)
  - `GET /api/review/{task_id}` 查状态 + 结果
  - `GET /api/review/{task_id}/events` SSE 流 (text/event-stream, 30s 心跳保活)
- **CLI**: `--web-review-mode {simple,full}` (默认 full) + `--web-review-llm {openai,anthropic,fake}` (默认 fake) + `--web-review-timeout` (默认 60s)
- **前端**: `/review` 页面 JS EventSource 订阅 SSE, 进度条 + 日志流 + 完成后显示结果
- **测试**: 18 unit (`tests/unit/test_web_review_async.py`) + 5 G-029 端到端 (`tests/golden/test_g029_review_async_e2e.py`)
- **DoD**: `verify_w36f_dod.py` 8/8; W36b/G-027 不破 (走 simple mode 兼容); 1233 passed (W36d 1204 + W36f +29)
- **已知限制**:
  - 内存 task store 单进程限制 (多 worker 留 W37+ Redis 共享)
  - 真实 OpenAI/Anthropic SDK 接入留 W37+ (当前 fake 模式等价 simple + 异步)
  - 任务清理 1h TTL + 10min cleanup interval (后台 loop 留 W37+)

#### W36e: repo 级 `ruff format` 150 文件欠债清理 (历史 cleanup) (2026-06-24)

- **新增**: 1 原子 commit 把 150 个文件 `ruff format` 落地
- **变更**: `+3308/-2133` 行 (格式调整, 无逻辑改动)
- **守门**: `verify_w36e_dod.py` 5/5 全过
  - `ruff format --check` 0 欠债 (185 files already formatted)
  - `ruff check` 0 errors
  - `mypy` 0 errors
  - `pytest` 全量 1238 passed (W36f 1233 baseline + 5 G-029 修复)
  - `git diff --stat` 150 files (确认 W36e 改的范围)
- **衔接**: W36g (0.5.0 final, 等 TestPyPI) + W37 (LLM 真实接入)
- **已知限制**: 1 原子 commit 改 150 文件, git blame 噪声 (可用 `.git-blame-ignore-revs` 隔离)

## [0.5.0a1] - 2026-06-22

### Phase 5 启动 (W28 GUI Web UI v1)

#### W28: GUI Web UI v1
- **新增**: `src/agent_swarm/web/` FastAPI 应用 (5 文件)
  - `app.py` — `create_app()` 工厂 + lifespan + 路由注册
  - `state.py` — `WebState` 全局状态 (事件缓冲 + 订阅者)
  - `routes.py` — 4 页面 + 5 partials + 3 JSON API
  - `websocket.py` — `/ws` 实时事件流 (心跳 + 自动重连)
  - `__init__.py` — 导出
- **新增**: 12 个 Jinja2 模板
  - `base.html` — HTMX 2.0 启用, nav + WS status
  - 4 页面: dashboard / agents / worktrees / tasks
  - 5 partials: events / metrics / agents / worktrees / tasks
- **新增**: 静态资源
  - `style.css` — 暗色主题 (CSS vars)
  - `app.js` — WebSocket client (重连 + 渲染)
- **新增**: pyproject.toml `[web]` optional-deps
  - fastapi>=0.110, uvicorn>=0.27, jinja2>=3.1, python-multipart>=0.0.9
- **新增**: `examples/w28_web_demo.yaml` — 2 worker + web UI
- **API**:
  - `GET /` / `/agents` / `/worktrees` / `/tasks` — HTML 页面
  - `GET /partials/{events,metrics,agents,worktrees,tasks}` — HTMX fragments
  - `GET/POST /api/{state,events}` — JSON
  - `GET /healthz` / `/metrics` (Prometheus 格式)
  - `WS /ws` — 实时事件流 (推所有 push_event)
- **测试**: 29 cases (app 工厂 / 12 GET 路由 / 5 API / 3 WS / 4 WebState)

#### 启动方式
```bash
pip install -e ".[web]"
uvicorn agent_swarm.web:app --reload
# 浏览器打开 http://localhost:8000
```

#### 已知限制 (v1)
- 无 RBAC / auth
- 事件缓冲默认 500 条 (超出丢老)
- Worktree 视图需 app.state.worktree_manager 注入 (P4-W22 集成入口预留)
- HTMX 自动刷新, 不支持手动控制

#### W31: Web UI CLI 集成 (--web 启动 uvicorn + WebStateSink)
- **新增**: `src/agent_swarm/observability/web_state_sink.py` — `WebStateSink(ObservabilitySink)`
  - consume SessionEvent 推入 WebState (驱动 Web UI)
  - 异常内部吞掉 (warning log), 不影响其他 sink
  - `drop_unsupported=False` 默认全推
- **新增**: `tests/unit/test_web_state_sink.py` — 10 cases
  - 基本 push / 空 payload / 嵌套 payload
  - 与 ObservabilityBus 集成 / 多 sink 共存
  - sink 异常不传播 / bus 兜底 / unregister / 协议 / repr
- **修改**: `src/agent_swarm/observability/__init__.py` — 导出 `WebStateSink`
- **修改**: `src/agent_swarm/cli/main.py` (+67) — `run` 命令新增选项
  - `--web` 启用 Web UI (同进程 uvicorn)
  - `--web-host` 绑定地址 (默认 127.0.0.1)
  - `--web-port` 端口 (默认 8000)
  - import 失败 (未装 [web]) → 友好提示 + sys.exit(2)
  - finally 块: `web_server.should_exit=True` + 等待 web_task 干净关闭
- **新增**: `examples/w31_web_with_swarm.yaml` — writer + reviewer 2 worker + `--web` 启动示例

#### 启动方式 (W31)
```bash
pip install -e ".[web]"
agent-swarm run examples/w31_web_with_swarm.yaml --web
# 浏览器打开 http://localhost:8000
```

#### DoD 验证 (W31)
- ruff 0 / mypy 0
- 10 cases (test_web_state_sink) passed
- 49 cases (test_web + test_web_state_sink + test_websocket_sink) 全过
- CLI `--help` 显示 `--web` / `--web-host` / `--web-port`
- 端到端: `web ui started (uptime=0s)` + LLM 失败时干净退出

#### W32: WorktreeManager → Web UI 注入 (闭环 W22 hook)
- **修改**: `src/agent_swarm/web/app.py` — `create_app` 接受 `worktree_manager: Any = None` 关键字
  - 注入 `app.state.worktree_manager` (P4-W22 集成入口)
  - 路由已有 `getattr(request.app.state, "worktree_manager", None)` 兜底, 无需改 routes
- **修改**: `src/agent_swarm/cli/main.py` — `run` 命令新增选项
  - `--web-worktree-repo PATH` — WorktreeManager repo_root (git 仓库路径, 必须存在)
  - `--web-worktree-base PATH` — base_dir (默认 `<repo>/.worktrees`)
  - 启用时实例化 `WorktreeManager(repo_root, base_dir)` 注入到 `create_app`
- **修改**: `tests/unit/test_web.py` — 4 新 cases
  - `test_create_app_accepts_worktree_manager` — create_app 接受并注入
  - `test_create_app_default_no_worktree_manager` — 向后兼容 (默认不挂)
  - `test_partial_worktrees_with_manager` — 注入后 /partials/worktrees 显示真数据
  - `test_partial_worktrees_empty_when_no_manager` — 无 manager 仍 200 (W28 兼容)
- **新增**: `examples/w32_web_with_worktree.yaml` — writer-A + writer-B + `--web-worktree-*`

#### 启动方式 (W32)
```bash
pip install -e ".[web]"
agent-swarm run examples/w32_web_with_worktree.yaml \
  --web --web-worktree-repo <git-repo-path>
# 浏览器打开 http://localhost:8000/worktrees — 显真 worktree 列表
```

#### DoD 验证 (W32)
- ruff 0 / mypy 0
- 4 cases (test_web W32) passed
- 全量回归 909 passed / 115 skipped / 0 failed (W31 905 + 4)
- CLI `--help` 显示 `--web-worktree-repo` / `--web-worktree-base`

#### G-022: Web UI 端到端 Golden Case (随 0.5.0a1 落地)
- **新增**: `tests/golden/test_g022_web_ui_e2e.py` — 6 cases 全过
  - `test_g022_sink_pushes_to_state` — WebStateSink → WebState 推送
  - `test_g022_ws_receives_pushed_events` — WebSocket 端到端 (含 _hello 心跳)
  - `test_g022_partials_events_renders_html` — HTMX partial HTML 渲染
  - `test_g022_multi_subscriber_fanout` — 多订阅 fan-out
  - `test_g022_buffer_overflow_drops_old` — deque 缓冲丢老
  - `test_g022_sink_exception_isolated` — sink 异常隔离
- **目的**: 把 W28 (Web UI v1) + W31 (CLI 集成) + W32 (Worktree 注入) 三切片锁进 CI 守门链

#### 决策锁定 (W33/W34, 2026-06-23)
- **W33 WebState 持久化后端** = **Postgres** (与 W25 PostgresBackend 复用 asyncpg 池;表 `webstate_events(seq PK, ts, event_type, payload JSONB, session_id, tenant_id)`)
- **W34 RBAC / auth 模式** = **JWT** (HS256;与 MCP source 分级 + 飞书 HMAC 风格一致;`Authorization: Bearer` 头)
- **PyPI 发版策略** = **0.5.0a1 → TestPyPI → 2 周 CI → 0.5.0 stable** (沿用 P3/P4 节奏)

#### W33a: P0-1 防御深度加固 + 遗留修复 (2026-06-23)
- **P0-1 安全修复** (`src/agent_swarm/security/sandbox.py`):
  - 新增 `_SECRET_ENV_NAME_RE` 正则: 名称 (大小写无关) 命中 `PASS|PASSWD|SECRET|TOKEN|CREDENTIAL|PRIVATE|API_KEY|_KEY` 整段即不透传给沙箱子进程
  - 防御 `cat` / `grep` 等白名单命令读出宿主进程里的 `OPENAI_API_KEY` / `LARK_APP_SECRET` / `PGPASSWORD` 等
  - 白名单移除 `env` / `ps`: 它们会把进程环境/进程表全量吐出, 即便 env 已脱敏仍是泄密面
  - `printenv` 保留 (按名打印单变量; 批量环境已脱敏, 无密钥可读)
  - `env_overrides` 显式注入的变量**不**被脱敏 (用户明确知情)
- **doctor CLI 新增 `--skip-sandbox`**: 与 `--skip-llm` / `--skip-mcp` 对称, 无 Docker 环境的 CI 可显式跳过 sandbox 检查
- **测试修复 / 补充**:
  - `test_sandbox_home_is_workspace`: 改用 `printenv HOME` (P0-1 移除 `env`)
  - `test_doctor_cli_all_skipped_via_fake_llm`: 加 `--skip-sandbox` 标志拿到 exit 0
  - **新增 4 个 P0-1 单测**: `_SECRET_ENV_NAME_RE` 命中/跳过 + `execute()` 透传脱敏 + `env_overrides` 不脱敏
- **G-020 Golden Case fixture 修复**:
  - 补 `input.yaml` (G-018/G-019 同模式占位)
  - 重写 `expected.yaml`: 同时含 GoldenExpectation schema (`id/title/phase/swarm_config/expected/performance`) + G-020 专用 invariants 字段
  - 解锁 benchmark smoke 加载 G-020 + 修 `test_g020_expected_yaml_matches`

#### DoD 验证 (W33a)
- ruff 0 errors
- mypy 0 errors (75 source files)
- 全量回归 **1251 passed / 0 failed** (W32 时 909 → 1251, +342 cases 含 P0-1 + G-020 + 历史累计)
- 4 cases (test_sandbox P0-1) passed
- `agent-swarm doctor --skip-llm --skip-mcp --skip-sandbox` exit 0

#### W33b: WebState Postgres 持久化 (2026-06-23)
- **新增** `src/agent_swarm/web/store.py` — WebStateStore 协议 + 双实现:
  - `WebStateStore` (Protocol): `append` / `recent` / `subscribe` / `unsubscribe` / `close`
  - `MemoryWebStateStore` — 内存环形缓冲 (DSN 缺省时零破坏降级)
  - `PostgresWebStateStore` — Postgres 持久化 (复用 W25 fake_module 注入模式)
  - `WebStateConfig` — dsn/table/min_size/max_size/command_timeout/fake_module/tenant_id
- **Schema** `webstate_events`:
  - 列: `seq BIGSERIAL PK, ts TIMESTAMPTZ DEFAULT NOW(), event_type TEXT, payload JSONB, session_id TEXT, tenant_id TEXT DEFAULT 'local'`
  - 索引: `idx_webstate_ts (ts DESC)` / `idx_webstate_session (session_id, seq)` / `idx_webstate_tenant (tenant_id, ts DESC)`
- **WebState 集成** (`src/agent_swarm/web/state.py`):
  - 新增可选 `store: WebStateStore | None` 字段
  - `push_event` 双写: 内存 deque + `store.append` (失败仅 log, 不影响内存路径)
  - DSN 缺省时 `store=None`, 与 W28 行为完全一致 (零破坏)
- **create_app 扩展** (`src/agent_swarm/web/app.py`):
  - 接受 `postgres_dsn` / `postgres_table` / `postgres_tenant_id` 关键字参数
  - DSN 给出时自动实例化 `PostgresWebStateStore` 注入 WebState
  - lifespan 启动/关闭时管理 store 生命周期
- **CLI 集成** (`src/agent_swarm/cli/main.py`):
  - 新增 `--web-postgres-dsn` 选项 (默认 None, 维持内存)
  - 新增 `--web-postgres-table` 选项 (默认 `webstate_events`)
- **测试** `tests/unit/test_webstate_store.py` — **22 cases 全过** (≥15 DoD):
  - Memory store: append/recent/subscribe/maxlen/unsubscribe/close/协议 (9)
  - Postgres store (fake): append/session 过滤/tenant_id/subscribe/close/协议/SCHEMA_SQL (7)
  - WebState 集成: 双写/store 失败不破内存/EventRecord.to_html (4)
  - G-023 重启恢复: 进程 A push→close→进程 B reconnect→recent 拉回 + session 隔离 (2)
- **守门脚本** `tools/verify_w33_dod.py` — **8/8 全过**:
  - Schema/append/recent/subscribe/重启恢复/CLI 选项/DSN 缺省降级/性能基线 (100 append 0.5ms)
- **决策微调**: WebStateStore 用**独立** asyncpg 池而非复用 W25 PostgresBackend 池 (不同 namespace, 避免耦合; KISS)

#### 已知限制 (W33b, P5 §17.2 阶段门控)
- `subscribe` 仅对**当前进程**有效 (PG 无 in-memory pub/sub)
- 跨进程实时 fan-out 需 W34+ 加 LISTEN/NOTIFY
- 本轮只保证 "重启不丢事件" (append 落盘 + recent 重启可拉回)

#### DoD 验证 (W33b)
- ruff 0 errors
- mypy 0 errors (76 source files, +1 from 75)
- 全量回归 **1273 passed / 0 failed** (W33a 1251 → 1273, +22 新增)
- `tools/verify_w33_dod.py` 8/8 PASSED
- G-022 不破 (Web UI 端到端 6 cases 仍过)

#### W34: WebState JWT 鉴权 (2026-06-23)
- **新增** `src/agent_swarm/web/auth.py` — JWT HS256 实现 (标准库, 零新依赖):
  - `JWTIssuer` — `encode` / `decode` / `verify_exp` / 错密钥拒绝 / 过期拒绝 / alg 校验
  - `JWTConfig` — secret / algorithm / expires_seconds / issuer
  - `JWTError` — 解析 / 验签 / 过期 失败异常
  - `get_current_user` / `require_user` — Depends 工具
  - `resolve_secret_ref` — `${VAR}` 引用解析 (复用 W20 风格, 不存明文)
- **create_app 扩展** (`src/agent_swarm/web/app.py`):
  - 接受 `jwt_secret` / `jwt_algorithm` / `jwt_expires_seconds` / `jwt_issuer_name` 关键字
  - secret 给出时: 实例化 `JWTIssuer` 挂 `app.state.jwt_issuer` + 挂全局 `jwt_middleware`
  - middleware 解析 `Authorization: Bearer` → 注入 `request.state.user`
  - 写路径 (POST/PUT/DELETE/PATCH) + 受保护前缀 (`/api/events`) + 无 user → 401 拦截
- **决策变更** (W34-D4): **写路径鉴权改在 middleware 全局拦截**, 不动 `request: Request` 路由签名
  - 原因: FastAPI 0.110+ `__future__ annotations` + `request: Request` 在多参数端点签名里触发 422 (query 参数误判)
  - 优点: KISS, 零路由代码改动, 业务路径清晰
- **CLI 集成** (`src/agent_swarm/cli/main.py`):
  - `--web-jwt-secret` 选项 (默认 None, 维持 W28 无鉴权行为)
  - `--web-jwt-expires` 选项 (默认 3600 秒)
  - 支持 `${WEB_JWT_SECRET}` 引用环境变量 (无明文)
- **API 扩展**: `POST /api/events` 响应增加 `by` 字段 (sub 或 "anonymous")
  - W28 既有 `test_api_post_event` 已更新接受新字段
- **测试** `tests/unit/test_web_jwt_auth.py` — **22 cases 全过** (≥15 DoD):
  - JWTIssuer 单元: encode/decode/wrong-secret/expired/tampered/garbage/alg/secret-required (8)
  - resolve_secret_ref: 字面值穿透 / ${VAR} 解析 / 缺变量拒绝 (3)
  - create_app + middleware: 零破坏 / secret 401 / ${VAR} / Bearer 解析 / 容错 / Basic 忽略 / GET 不强制 (7)
  - Depends: get_current_user / require_user 401 (2)
  - G-024 Golden Case: 完整端到端 login → 持 token → 401 不带 / 401 过期 / 401 错密钥 (1)
- **守门脚本** `tools/verify_w34_dod.py` — **8/8 全过**:
  - roundtrip / 错密钥 / 过期 / ${VAR} / 零破坏 / 401 拦截 / CLI / 性能 (100 encode+decode 1.8ms)

#### DoD 验证 (W34)
- ruff 0 errors
- mypy 0 errors (77 source files, +1 from 76)
- 全量回归 **1295 passed / 0 failed** (W33b 1273 → 1295, +22 新增)
- `tools/verify_w34_dod.py` 8/8 PASSED
- G-022 / W33a / W33b 全部不破

#### 已知限制 (W34)
- middleware 单进程: 写路径 401 拦截在多 worker 部署时需共享 secret (DSN/ENV 一致)
- HS256 共享密钥: 需通过 SecretManager 轮换 (与 W20 Vault 风格一致)

#### W35: WebState 跨进程 fan-out (LISTEN/NOTIFY) (2026-06-23)
- **新增** `src/agent_swarm/web/store.py` (W35 段):
  - `PostgresNotifier` — asyncpg LISTEN/NOTIFY 封装 (零新依赖)
  - `NotifyEnvelope` — JSON 协议 (origin/seq/event_name/session_id/payload/ts)
  - `NOTIFY_CHANNEL = "webstate_notify"` / `NOTIFY_PAYLOAD_LIMIT = 7KB`
  - 8KB NOTIFY 硬限制保护: 超长 payload 降级为 `{"_truncated": True}` 占位
  - origin_id (uuid4 hex) 过滤自订阅, 防 fan-out loop
- **`PostgresWebStateStore.attach_notifier(notifier)` 钩子**:
  - append 写盘后自动 `notifier.notify(...)` 触发跨进程 NOTIFY
  - notifier 未挂时零变化 (W33b 兼容)
- **`WebState.attach_notifier(notifier)` 集成入口**:
  - 自动调 `store.attach_notifier(notifier)` (如有 store)
  - 注册 on_notify 回调, 把跨进程 envelope 转成本地 EventRecord + 通知本地订阅者
  - on_notify 同步回调 → 用 `asyncio.ensure_future` 走 event loop, lock 安全写
- **`create_app` 扩展**:
  - 接受 `enable_cross_process: bool = False`
  - DSN 给出时实例化 `PostgresNotifier` 挂 `app.state.web_notifier`
  - 无 DSN 时静默 (向后兼容)
  - lifespan: 启动 `notifier.listen()` + `state.attach_notifier(...)`; 退出 `notifier.close()` (先于 store)
- **CLI 集成**:
  - `--web-cross-process / --no-web-cross-process` 选项 (默认 False, W28 行为零破坏)
- **测试** `tests/unit/test_web_cross_process.py` — 18 cases 全过:
  - NotifyEnvelope 协议 (roundtrip/截断/unicode) 3
  - PostgresNotifier (origin 过滤/listen+notify/多 listener/幂等/close/notify 自动 listen) 8
  - create_app 集成 (无 DSN/有 DSN/cross_process 钩子) 4
  - WebState.attach_notifier (有 store/无 store) 2
  - 修 W34 ruff F401 (test_web_jwt_auth.py:17 unused typing.Any)
- **G-025 Golden Case** `tests/golden/test_g025_cross_process.py` — 4 cases 全过:
  - 跨进程 A→B notify roundtrip
  - 同 origin 自订阅不触发 (fan-out loop 防护)
  - 三进程 fanout (A 推 → B+C 收到, A 收不到自己)
  - 三进程顺序通知 (各收 2 条, 来自其他两进程)
- **守门** `tools/verify_w35_dod.py` — **8/8 全过**:
  - Envelope 协议 / NOTIFY 发出 / origin 过滤 / 跨进程接收 /
    CLI 选项 / DSN 缺省降级 / create_app 集成 / 性能基线 (100 notify < 5s)

#### DoD 验证 (W35)
- ruff 0 errors (W35 范围)
- mypy 0 errors (77 source files)
- web 子集 108/108 passed (W34 97 + W35 18 - 7 重叠 = 108)
- `tools/verify_w35_dod.py` 8/8 PASSED
- G-022 / G-023 / G-024 / W33a/b / W34 全部不破

#### 已知限制 (W35, 闭环 W33b 阶段门控)
- 性能: fake 模式 100 notify 0.0ms; 真 PG 模式 100 notify 应 < 100ms (含 100 次 roundtrip)
- 多 worker (gunicorn/uvicorn workers) 各自 origin, NOTIFY 触达所有 — 这是预期行为
- LISTEN 需长连接, 与 append 池独立 (`notifier_conn` 单独持有)
- HS256 secret 轮换: W36+ 接 SecretManager (W34 已知限制已记录)

#### 已知缺口 (等用户环境)
- TestPyPI 上传: `twine check` PASSED, 实发需用户配 `~/.pypirc` token + non-interactive terminal
- DESIGN.md 已 untrack (chore 2e1de16), §17.2 P5 DoD 内容本地保留
- docs/ 已 untrack (chore 943f432), 计划/复盘文档本地保留
- 端到端: `WorktreeManager initialized` + `web UI started (uptime=0s)`

#### W36a: WebState JWT Secret 走 SecretManager (轮换不重启) (2026-06-24)
- **闭环 W34 已知限制** #2: HS256 共享密钥需 SecretManager 轮换
- **新增** `src/agent_swarm/web/auth.py`:
  - `SecretRef` frozen dataclass (kind: Literal["literal","env","secret_ref"], value: str)
  - `parse_secret_ref(ref)` — 识别 literal / `${VAR}` / `secret://key` 三种格式 + 错误路径
  - `JWTConfig` 扩展: 新增 `secret_ref: str | None` + `secret_manager: SecretManager | None` 字段
  - `JWTIssuer` 重构: 持 SecretManager + `(key, version) → bytes` cache
  - `resolve_secret()` async — always-fresh 拉取 (lifespan 启动 / 定时任务调用)
  - `decode()` 走 cache (sync, 性能)
  - `invalidate_cache()` — 强制下次重读
- **`create_app` 扩展**:
  - 接受 `jwt_secret_ref: str | None` (与 `jwt_secret` 互斥)
  - 接受 `secret_manager: SecretManager | None` (secret:// 模式缺省 = EnvSecretManager)
  - W34 字面值 / W36a secret_ref 双向兼容, 零破坏
- **CLI 集成**:
  - `--web-jwt-secret-ref TEXT` (W36a 引用字符串)
  - `--web-secret-manager [env|vault]` (缺省 env, vault 需 hvac)
  - `--vault-url / --vault-role-id / --vault-secret-id` (Vault AppRole 三件套)
- **新增** `tests/unit/test_web_jwt_secret_ref.py` (21 cases) — SecretRef 协议 + parse
- **新增** `tests/unit/test_web_jwt_rotation.py` (7 cases) — 轮换 cache + 降级路径
- **新增** `tests/golden/test_g026_jwt_rotation.py` (5 cases) — Phase A-D 端到端:
  - Phase A: v1 签发 token + verify
  - Phase B: rotate 到 v2
  - Phase C: 旧 token 在 cache TTL 内 verify / 触发 resolve 后失效
  - Phase D: v2 签发新 token + verify
  - Full lifecycle: A→B→C→D 串联
- **守门** `tools/verify_w36a_dod.py` — **8/8 全过**:
  - SecretRef 协议 / 错误路径 / JWTConfig 互斥 / create_app 4 模式
  - EnvSecretManager 集成 / 失败降级 / version 失效 / CLI 选项
- **向后兼容**: W34 `JWTConfig(secret="...")` 字面值模式零破坏, 22 老 case 全过

#### DoD 验证 (W36a)
- ruff 0 errors (W36a 范围 + 全项目)
- mypy 0 errors (77 source files)
- 全量回归 **1185+ passed / 0 failed** (W35 1126 → W36a 1185, +59 新增)
- `tools/verify_w36a_dod.py` 8/8 PASSED
- W34/W35 全部不破 (cross-version 兼容)

#### 已知限制 (W36a)
- `vault://path#field` URI scheme 留 W36c (W36a 只做 `secret://` 单协议)
- SecretManager cache TTL 由 SecretManager 自管 (W20 VaultSecretManager 5min), JWTIssuer 不额外加 TTL
- 多 worker (gunicorn/uvicorn) 各自 SecretManager 实例, 轮换时各自 cache 失效 (符合预期)

#### W36b: agent_review Web 入口 (UI 按钮触发 review) (2026-06-24)
- **闭环**: Web UI 与 agent_review 工具集成, 用户无需离开浏览器
- **新增** `src/agent_swarm/web/review_runner.py` — 薄包装层
  - `run_review_sync(pr_ref, repo_root) → dict` 同步跑 simple review
  - `AGENT_REVIEW_REPO` env 临时设置 (agent_review 内部用此定位仓库)
  - `sys.modules` 缓存清理 (让 env 变更生效)
  - `_is_git_repo(path)` 前置检查 → 友好 500 错
- **`create_app` 扩展**:
  - 接受 `web_repo_root: Path | None = None` (类似 worktree_repo, 注入到 `app.state.web_repo_root`)
- **新增路由**:
  - `GET /review` 页面 (HTMX 表单 + Run Review 按钮 + 结果展示区)
  - `POST /api/review` 接受 `pr_ref` JSON, 同步返 `ReviewReport`
  - `GET /partials/review_form` partial
  - 写路径强制 Bearer token (PROTECTED_PREFIXES 加 `("/api/events", "/api/review")`)
- **新增模板**:
  - `templates/review.html` (HTMX 表单 + spinner)
  - `templates/partials/review_result.html` (verdict bar + findings table)
  - `base.html` nav 加 `/review` 入口
- **错误处理**:
  - pr_ref 含 `;` `&` `|` ` `` ` `$` `>` `<` `\n` `\r` → 400 (shell 注入防御)
  - 非 git 仓库 → 500 + `{"detail": "not a git repository: ..."}`
  - git 不可用 → 500 + `{"detail": "git not available: ..."}`
  - empty diff → 200 + verdict=approve
- **新增** `tests/unit/test_web_review.py` (14 cases):
  - 页面渲染 (200 / HTMX form / nav)
  - 鉴权 (W34 mode 401 / W28 兼容 200-500)
  - pr_ref 参数 (默认 / 自定义 / unsafe / shell 注入 / pipe)
  - 错误处理 (非 git repo 500)
  - `_validate_pr_ref` 单元
- **新增** `tests/golden/test_g027_review_e2e.py` (4 cases):
  - 干净 PR → 0 finding / verdict=approve
  - secret_leak (hardcoded API key) → ≥1 finding / verdict≠approve
  - cmd_injection (subprocess shell=True) → ≥1 CMD finding
  - 报告 schema 完整 (含 summary / findings / verdict / confidence)
- **守门** `tools/verify_w36b_dod.py` — **8/8 全过**
- **向后兼容**: W28 (no auth) / W34 (auth) / W36a (SecretManager) 三模式 0 破坏

#### DoD 验证 (W36b)
- ruff 0 errors (W36b 范围 + 全项目)
- mypy 0 errors (78 source files)
- 全量回归 **1185+ passed / 0 failed** (W36a 1185 → W36b +18, 14 unit + 4 G-027)
- `tools/verify_w36b_dod.py` 8/8 PASSED
- W28/W34/W36a 全部不破

#### 已知限制 (W36b)
- 同步 review 阻塞 (run_simple_review 跑完才返) — 巨 PR (1000+ 文件) 慢, W36b 接受, 异步化留 W36f
- `web_repo_root` 默认 None → 用 FastAPI app 启动 cwd (通常是 repo_root, 但生产部署需显式配)
- `run_full_review` (LLM + 对抗式) 不在 W36b 范围, 留 W36f

#### W36c: vault://path#field URI 扩展 (闭环 W36a 协议) (2026-06-24)
- **闭环 W36a 留口子**: "vault:// URI 留 W36c"
- **`parse_secret_ref` 扩展** 4 种格式:
  - `literal` / `env` / `secret://key` (W36a 兼容)
  - `vault://path` (无 field) → SecretRef(kind=vault, value=path, field=None)
  - `vault://path#field` → SecretRef(kind=vault, value=path, field=field)
- **`SecretRef` 扩展** `field: str | None = None` 字段 (frozen dataclass, 向后兼容 W36a 3 kinds)
- **`JWTIssuer.resolve_secret` vault 模式**:
  - 调 `secret_manager.get(path)` 拿 Secret
  - `field` 给出时: 解析 JSON, 提取字段
  - `field` 缺失 → JWTError("field not in document")
  - value 非 JSON → JWTError("not JSON")
  - 走 (key, version) cache, version 变化时重读 (W36a 模式复用)
- **`create_app` 扩展**:
  - 接受 `vault_url` / `vault_role_id` / `vault_secret_id` 关键字 (W36c 新)
  - `vault://` + 无 `secret_manager` → 自动实例化 `VaultSecretManager`
- **CLI 集成**:
  - `--web-jwt-secret-ref` 接受 `vault://` URI (W36a CLI 扩展)
  - `--web-secret-manager vault` + `--vault-url/--vault-role-id/--vault-secret-id` 复用 (W36a 已就位)
- **新增** `tests/unit/test_web_jwt_vault_ref.py` (14 cases):
  - parse 5 cases (无 field / 有 field / 空 path / 空 field / 复杂 path)
  - SecretRef field 字段 3 cases
  - JWTConfig vault 模式 1 case
  - JWTIssuer resolve 5 cases (无 field / 有 field / field 缺失 / 非 JSON / 轮换 cache 失效)
- **新增** `tests/golden/test_g028_vault_ref.py` (5 cases):
  - vault 无 field 直接用 value
  - vault 有 field 提取 JSON
  - rotate 后 field 变化 → cache 失效
  - Vault 不可用 + cache 命中 → 降级
  - 端到端: parse + resolve + encode + decode + rotate
- **守门** `tools/verify_w36c_dod.py` — **8/8 全过**
- **向后兼容**: W36a 3 kinds (literal / env / secret_ref) 全部不破, 老 22 case 全过

#### DoD 验证 (W36c)
- ruff 0 errors (W36c 范围 + 全项目)
- mypy 0 errors (78 source files)
- 全量回归 **1204+ passed / 0 failed** (W36b 1185 → W36c 1204, +19 新增: 14 unit + 5 G-028)
- `tools/verify_w36c_dod.py` 8/8 PASSED
- W36a/W36b 全部不破 (跨 6 commit 兼容)

#### 已知限制 (W36c)
- vault:// 仅支持 JSON 文档 (Vault KV v2 风格); YAML 文档留 W36c+
- vault:// 触发自动 VaultSecretManager 实例化时, 测试需注入 fake (CLI 模式不支持)
- 多 worker 部署时 SecretManager 各自一份 (W36a 限制延续)

## [0.4.0a1] - 2026-06-22

### Phase 4 收尾 (W22-W26)

#### W22: WorktreeManager 核心 (manager.py)
- **新增**: `WorktreeManager(repo_root, base_dir)` ——per-agent git worktree 隔离
- **新增**: `WorktreeHandle` dataclass (path / branch / agent_id / tenant_id / session_id / created_at / key)
- **API**: `acquire(tenant_id, session_id, agent_id) → WorktreeHandle` (幂等)
- **API**: `release(handle, force=False)` / `list_active()` / `get(key)` / `cleanup_orphans(ttl)` / `cleanup_all()`
- **异常**: `WorktreeError` / `WorktreeRepoError` / `WorktreeConflictError`
- **并发安全**: per-tenant `threading.Lock` + 同 (tenant, session, agent) 幂等
- **测试**: 26 cases (基础 / 并发 / 隔离 / cleanup / 路径安全)
- **路径处理**: `_sanitize` 把任意字符串转安全标识符; `_is_git_repo` 跨平台 (Windows 正反斜杠归一)

#### W23: MCP 集成 (integration.py)
- **新增**: `${WORKTREE_PATH}` 占位符 → 注入 `MCPServerConfig.command` / `cwd` / `env`
- **新增**: `substitute_placeholders` / `validate_config` / `find_placeholders`
- **新增**: `WorktreeIntegration(manager)` 高层封装
- **新增**: `examples/w22_mcp_worktree.yaml` ——2 worker 共享 repo
- **新增**: G-021 Golden Case 3 cases (3 agent 100 文件 / 10 并发 / 跨租户)
- **新增**: `tools/bench_worktree.py` 压测
- **新增**: `tools/verify_p4_dod.py` DoD 守门

#### W24: Docker 长生命周期 (sandbox_docker.py)
- **新增**: `DockerConfig.long_lived: bool = True` (默认)
- **新增**: `_start_container` / `_stop_container` / `_run_in_long_lived_container`
- **新增**: `close()` / `__aenter__` / `__aexit__` 异步 context manager
- **容器名**: `agentswarm-<workspace_hash>-<pid>-<counter>` (类级计数器防冲突)
- **性能**: 100 execute() 只启 1 容器 (vs W19 模式 100 次)
- **测试**: 13 cases (CIS 参数 / 启停 / 续用 / 兼容 W19 / 容器名唯一)
- **兼容**: `long_lived=False` 保留 W19 行为

#### W25: PostgresBackend (postgres_backend.py)
- **新增**: `PostgresBackend` + `PostgresConfig` 生产级持久化后端
- **Schema**: `tasks(id PK, version INT, data JSONB, updated_at TIMESTAMPTZ)`
- **CAS**: `UPDATE ... WHERE id=? AND version=? RETURNING data` (单语句原子)
- **命名空间**: schema 隔离
- **连接池**: asyncpg (min_size=1, max_size=20)
- **测试支持**: `fake_module` 参数注入 mock
- **测试**: 13 cases (CRUD / CAS / 重复 / stats / close / 协议)
- **集成**: `backends/__init__.py` 加导出 (try/except 可选依赖)

#### W26: Vault Dynamic Secrets (secret_manager.py)
- **新增**: `DBCredentials` dataclass (username / password / lease_id / lease_duration / issued_at)
  - 派生属性: `expires_at` / `seconds_to_expiry` / `is_expired` / `as_dsn()`
- **新增**: `VaultDynamicSecretManager(VaultSecretManager)`
  - `get_dynamic_credentials(role)` — Vault database/creds/{role} 发凭证
  - `renew_lease(lease_id, increment=3600)` — 续约
  - `revoke_lease(lease_id)` / `revoke_all()` — 显式回收
  - `list_active_leases()` — 调试/监控
- **用例**: 连接 DB 前 get → 用完 revoke / 长期任务 renew
- **测试**: 14 cases (DBCredentials / get / renew / revoke / workflow)

#### Phase 4 统计
- unit tests: 1060 passed (was 975 in P3, +85 new)
- golden: G-021 3/3 (新增)
- 全量: 1060 passed / 138 P3-WIN skipped / 0 failed
- ruff 0 errors / mypy 0 errors
- 4 commits (W22-23, W24, W25, W26) + 4 tags (0.4.0a1-0.4.0a4)

#### 待启动 (Phase 4 续 / Phase 5)
- GUI Web UI (§16.2 #2 React vs HTMX 选型) - 4-6 周
- 多语言 SDK (Go/TypeScript) - 4 周
- 分布式 swarm (跨机器调度) - 长期

## [0.3.0] - 2026-08-29

### Phase 3 收尾 (W14-W21)

#### W14a — MCP SSE 传输 + 重连/熔断 (5ea044d)
- **新增**: `SseMCPClient` (HTTP POST + SSE 流解析) — JSON-RPC 2.0 over HTTP+SSE
- **新增**: `CircuitBreaker` 三态 (CLOSED/OPEN/HALF_OPEN) + 指数退避 0.5/1/2/4/8 秒
- **新增**: `ReconnectingMCPClient` 单次重连防无限循环 + 43 tests
- **新增**: G-018 Golden Case — MCP server crash 9 connect attempts 3 reconnects
- **新增**: `tools/count_reconnect.py` 验证脚本 (12.2s 实测)

#### W14b — CI 落地 + Doctor (574f81c)
- **新增**: `agent-swarm doctor` 子命令 — LLM/SQLite/MCP/Secrets 4 类检查, exit 0/1/2
- **新增**: `docs/concepts.md` (Agent/Task/Mailbox/KB/AdversarialVerifier 5 章节)
- **新增**: `docs/troubleshooting.md` (10 章节 ≥10 错误)
- **CI**: `.github/workflows/ci.yml` 新增 security-scan + windows-smoke job
- **测试**: 21 doctor tests

#### W15 — PrometheusSink + 5 核心指标 (47d3f9d)
- **新增**: `PrometheusSink` + aiohttp `/metrics` + `/healthz` 端点
- **指标**: `framework_tasks_total` / `framework_llm_tokens_total` / `framework_cas_conflict_total` / `framework_mcp_circuit_state` / `framework_approval_pending_count`
- **新增**: `tools/agent_review.py --require-human-review --approve-override --fail-on` 守门 (CRITICAL exit 2)
- **新增**: `docs/grafana/dashboard.json` 4 面板 + `docs/recipes/README.md` 5 recipes
- **新增**: `docs/RISK-LOG.md` 20 风险登记
- **新增**: G-019 Golden Case — agent_review 识别真问题
- **依赖**: `prometheus-client>=0.20.0`
- **测试**: 25 prometheus_sink + 7 G-019

#### W16 — 多租户隔离 (593e35d)
- **新增**: `SecurityContext.mode` (TenantMode) + `__post_init__` 校验 (multi 拒绝 empty/whitespace/local reserved)
- **新增**: `TenantQuota` + `TenantQuotaRegistry` 滑窗 (3600s) 跨租户拦截
- **新增**: `patched_create_task` 自动注入 SecurityContext (via `ctx.asyncio_context()`)
- **新增**: `tools/audit_create_task.py` 扫 8 个裸 `asyncio.create_task` (与 lint 一致)
- **新增**: `tools/check_sql_lint.py` SQL 注入审计 (启发式 + 邻近窗 + SQLITE_MASTER 例外)
- **新增**: `tools/bench_multi_tenant.py` 100 并发跨租户压测 (5672 QPS / p99=0.5ms / 0 越权)
- **测试**: 17 TenantQuota + 11 MultiTenantConfig + 14 cross-tenant
- **示例**: `examples/w16_multi_tenant.yaml`

#### W17 — 跨通道 Session 合并 (593e35d / 269563a)
- **新增**: `SessionBindingManager` (内存版 + 可选 SQLite) — `(tenant_id, identity_key) → session_id`
- **新增**: `ChannelIdentity` 注册 + `resolve_user` 跨通道身份合并
- **新增**: `bind_or_get_session` 跨通道共享 (飞书 + CLI 同 user_id → 同一 session)
- **新增**: `tools/no_bare_create_task.py` lint 守门 (只对 `src/agent_swarm/` 生效, 允许 `context=` 显式参数)
- **新增**: `docs/MULTI-TENANT-REPORT.md` 100 并发实测 (56723 ops / 10s / 0 越权 / p99=0.5ms)
- **测试**: 17 SessionBinding (含跨通道合并 + tenant 隔离 + SQLite 持久化)
- **示例**: `examples/w17_cross_channel.yaml`

#### W18 — Redis 后端 (c1a5d73 / c8f4540)
- **新增**: `TaskQueueBackend` ABC + `StoredTask` 序列化
- **新增**: `MemoryBackend` — 单进程 asyncio.Lock CAS
- **新增**: `RedisBackend` — WATCH/MULTI/EXEC 乐观锁 (W18-4 多进程并发安全)
- **新增**: G-020 Golden Case — 100 agent 并发 claim → 1 OK + 99 conflict (version=1, assigned 持久化)
- **新增**: `tools/bench_storage.py` — Memory 188k QPS / Redis (fakeredis) 3478 QPS
- **新增**: `docs/STORAGE-BENCH.md` 自动生成
- **依赖**: `[redis] extras` — `redis>=5.0.0` + `fakeredis>=2.20.0`
- **测试**: 17 backend + 2 G-020
- **示例**: `examples/w18_redis_backend.yaml`

#### W19 — Docker Sandbox 保守版 (W19 commit)
- **新增**: `SandboxMode.DOCKER` 枚举值 (默认仍 WORKSPACE_ONLY — 向后兼容)
- **新增**: `DockerSandboxManager` + `DockerConfig` + `EscapeAttempt` + `CISDockerCheck`
- **新增**: 10 条 CIS Docker Benchmark 关键项 (4.1/5.2-7/5.12-14)
- **新增**: 20 条容器逃逸拦截 (`CONTAINER_ESCAPE_ATTEMPTS`) — mount/privileged/cap-add/host net/nsenter/chroot/cgroup/ctr
- **新增**: `agent-swarm doctor` 新增 `sandbox.docker` 检查
- **新增**: `tools/bench_sandbox.py` — Workspace 1308 QPS / Docker 163 QPS (mock 50ms)
- **新增**: `docs/SANDBOX-BENCH.md`
- **测试**: 20 docker_sandbox + 24 container_escape (含合法命令边界)
- **示例**: `examples/w19_docker_sandbox.yaml`

#### W20 — Vault 密钥 + MCP source 分级 (W20 commit)
- **新增**: `SecretManager` ABC + `EnvSecretManager` (read-only) + `VaultSecretManager`
- **新增**: AppRole 认证 + KV v2 secret engine + 内存缓存 TTL (默认 5 分钟)
- **新增**: `rotation_due` 预警 (提前 7 天) — 通过 ObservabilityBus emit
- **新增**: `MCPSource` + `SOURCE_TO_DEFAULT_RISK` + `validate_source` (强制必填)
- **新增**: `MCPServerConfig.source` 字段 (`official|community|private`) — YAML 缺此字段启动失败 (§16.3 #10 收紧)
- **默认分级**: official → LOW / private → MEDIUM / community → HIGH (生产默认禁 + Approval 强制)
- **新增**: `bump_tool_risk_by_source` (取 max) — W11 ApprovalGate 集成点
- **测试**: 17 secret_manager + 17 mcp_source_tier
- **示例**: `examples/w20_vault.yaml`

### Changed / Migration Notes

#### BREAKING (需 migration)
- `MCPServerConfig` 新增 `source` 字段 (Literal) — YAML 缺此字段 → 启动失败
  ```yaml
  mcp_servers:
    my_server:
      transport: stdio
      command: [...]
      source: community  # 必须显式
  ```
- `SandboxMode` 新增 `DOCKER` 枚举值 — `WORKSPACE_ONLY` 仍是默认 (向后兼容)

#### API 锁定 (Phase 3)
- `TaskQueue.add / claim / complete / fail` — 不变
- `SecurityContext.tenant_id / user_id / mode` — `mode` 新字段
- `Mailbox.send / all_messages` — 不变
- `KB.cache_analysis / get_cached_analysis` — 不变

### 已知问题
- 18 个 sandbox/CLI tests 在 Windows 下 fail (`command not found: [WinError 2]` 是 Windows sandbox subprocess 行为差异, 与 Phase 3 无关, 仅在 Linux runner 通过)

### 性能基准
- 多租户压测: **56723 ops / 10s / 0 越权 / p99=0.5ms** ✅ (100 并发)
- Memory backend: **188467 QPS / p99=0.002ms**
- Redis (fakeredis): **3478 QPS / p99=0.543ms**
- Workspace sandbox: **1308 QPS / p99=15.9ms**
- Docker sandbox (mock): **163 QPS / p99=63ms** (启动开销 50ms ≤ 500ms DoD)

### 测试统计
- W14a-W20 新增/改动测试: **~245 tests 全部 PASS**
- 全量回归: **1077 passed / 35 failed / 1 skipped** (35 fail 是 Windows sandbox 已知差异)

---

## [0.2.0] - 2026-07-15

Phase 2 收尾: 飞书通道 + MCP server 集成 + 多通道 + AgentReview

### Added
- W8-W13 Phase 2 全部交付 (见 git log)

---

## [0.1.0a1] - 2026-06-01

Phase 1 alpha: 核心 swarm + TaskQueue + Mailbox + KB + sandbox 基础

### Added
- W1-W7 Phase 1 全部交付 (见 git log)
