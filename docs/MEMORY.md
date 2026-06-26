# agent-swarm 项目 MEMORY

> 本地保留,不入 Git (.gitignore 已 untrack `docs/`)
> 按 [[pdca-dev-cycle]] A 阶段:每轮 PDCA 闭环时新增 1 条
> 格式: 关键经验 + 风险落地 + link 到代码位置

---

## W35 — WebState 跨进程 fan-out (LISTEN/NOTIFY)

**关键经验:**

1. **NOTIFY 8KB 硬限制** — PostgreSQL NOTIFY payload 上限 8000 字节
   - 工程化:协议层阈值设 7KB,超长降级为 `{"_truncated": True, "size": N}` 占位
   - 业务层不能假设 envelope 完整 — 消费方需容忍 truncated
   - 来源:`src/agent_swarm/web/store.py` `NOTIFY_PAYLOAD_LIMIT = 7 * 1024`

2. **origin_id 防 fan-out loop** — 跨进程 fan-out 的经典坑
   - 每个进程生成 uuid4 hex 32 字符 origin_id
   - on_notify 时 `if env.origin == self.origin_id: return` 直接丢
   - 不做这层 → 进程 A 发的 NOTIFY 被自己 LISTEN 收到 → 触发本地 store.append → 再发 NOTIFY → loop
   - 来源:`src/agent_swarm/web/store.py` `PostgresNotifier.origin_id`

3. **asyncpg LISTEN 独占长连接** — 与连接池模式冲突
   - 解法:独立 `notifier_conn` 单连接持有,池 `pool` 走 append
   - 关闭顺序:notifier 先 close → store 后 close (避免 store 写入时 notifier 已断)
   - 来源:`src/agent_swarm/web/app.py` lifespan 反序关闭

4. **fake asyncpg bus 模拟"两进程"** — Golden Case G-025 模式
   - 测试不真起两个进程,用 `_FakeAsyncpgBus(notifiers: list)` 共享一个 in-memory 队列
   - 各 notifier 调 notify() 时,其他 notifier 的 listener 收到 (跨进程语义)
   - 各 notifier 自己 notify 时,自己的 listener **不**收 (origin 过滤语义)
   - 这套 fake 是 W33 PG Store 模式的延伸,后续跨进程测试可复用
   - 来源:`tests/unit/test_web_cross_process.py` + `tests/golden/test_g025_cross_process.py`

5. **DSN 缺省降级零破坏** — W28 行为零变化原则
   - `enable_cross_process=True` 但无 DSN → 静默 (向后兼容)
   - 不抛错,不打 warning (避免噪声)
   - 真要 NOTIFY 走 PG,但没 DSN 才是用户配置错
   - 来源:`src/agent_swarm/web/app.py` `create_app(enable_cross_process=...)`

**风险落地 (下轮关注):**

- 性能:fake 模式 100 notify 0.0ms;真 PG 模式应 < 100ms (W35 守门基准)
- 多 worker (gunicorn/uvicorn workers) 各自 origin — 预期行为,文档化
- LISTEN 长连接 + append 池独立 — 资源占用 +1 连接,部署需注意

**W36 衔接:** W34 已知限制 "HS256 secret 需 SecretManager 轮换" → W36a 接 SecretManager

---

## W36a — WebState JWT Secret 走 SecretManager (轮换不重启)

**关键经验:**

1. **SecretRef 协议三态** — string 类型安全的优雅扩展
   - `literal`: 字面值 (W34 兼容, ref 字符串本身就是 secret)
   - `env`: `${VAR}` 引用 (W34 兼容, 调用方 resolve)
   - `secret_ref`: `secret://key` SecretManager 引用 (W36a 新)
   - 协议层加 kind 字段, 不破坏调用方 — `parse_secret_ref(ref) → SecretRef(kind, value)` 统一入口
   - 来源:`src/agent_swarm/web/auth.py` `parse_secret_ref`

2. **resolve_secret always-fresh 语义** — 与"decode 走 cache"分层
   - `resolve_secret()` async, 每次都打 SecretManager.get (version 校验 + cache 更新)
   - `decode()` sync, 走 cache (性能)
   - cache 失效靠 `SecretMetadata.version` 变化驱动, 不靠 TTL (避免时钟漂移)
   - 设计: lifespan 启动调 `resolve_secret()` 初始化; 定时任务可调刷新; rotate 后调 `invalidate_cache()` 强制重读
   - 来源:`src/agent_swarm/web/auth.py` `JWTIssuer.resolve_secret`

3. **降级路径二态** — 区分 cache 命中 / cache miss
   - `SecretManager.get` 失败 + cache 命中 → warning log + 继续用旧 secret (不破, 业务短时可用)
   - `SecretManager.get` 失败 + cache miss → `JWTError` (硬错, 不静默)
   - 原则: 优先不破 → 真扛不住才报错 (P0 防御深度风格)
   - 来源:`src/agent_swarm/web/auth.py` resolve_secret 的 try/except

4. **JWTConfig 互斥校验** — dataclass `__post_init__` 模式
   - `secret` 与 `secret_ref` 互斥 (用户不能两都给)
   - 至少一个必给 (避免空 config 通过)
   - 校验放在 `__post_init__`, 构造时即失败, 不延迟到运行时
   - 模式可推广到所有 config 升级: 新字段时, 用互斥 + 必给校验避免歧义
   - 来源:`src/agent_swarm/web/auth.py` `JWTConfig.__post_init__`

5. **W34 兼容零破坏** — 老路径继续工作
   - `JWTConfig(secret="x")` W34 字面值模式 100% 不变
   - `jwt_secret="${VAR}"` env 引用 W34 路径继续工作
   - 新字段都是 default None, 现有 22 个 W34 test case 不破
   - 守门 8 项中第 3 项专门验: 互斥 + 必给 + 老路径不变
   - 模式: 扩展 dataclass 时所有新字段给 default, `__post_init__` 校验互斥

6. **fake SecretManager 测试模式** — Protocol + fake 实现 + 故障注入
   - W36a 的 fake 比 W26 多 2 个能力: `fail_get` 故障注入 + `get_count` 监控
   - 测试能验 "cache 命中 (get_count 不变)" / "version 变化 (cache 重读)" / "Vault 宕机 (降级)" 三种语义
   - 这是 W33 PG Store fake 模式的延伸, 后续 cross-cutting 测试可复用
   - 来源:`tests/unit/test_web_jwt_rotation.py` `_FakeSecretManager`

7. **CLI 增量扩展** — Click 装饰器堆叠
   - 新选项加在老选项后面, 不动老选项的 default/help
   - `--web-secret-manager` 用 `click.Choice(["env", "vault"])` 限定
   - vault 三件套 (`--vault-url` / `--vault-role-id` / `--vault-secret-id`) 都是 str, 缺省值给个合理默认 (url=127.0.0.1:8200)
   - 模式: CLI 升级时老选项 default/help 绝不动, 只加新选项
   - 来源:`src/agent_swarm/cli/main.py` `run` 命令

**风险落地 (下轮关注):**

- 多 worker 部署时 SecretManager 各自一份 → 轮换时各 cache 失效时间不一致 (W36a 接受, 文档化)
- `vault://` URI 留 W36c, 不在 W36a 范围 (避免一刀切)
- SecretManager cache TTL 不在 JWTIssuer 层 (SecretManager 自管, 与 W20 一致)

**W36b 衔接:** agent_review Web 入口 (UI 按钮 → 触发 review) 是 W36 候选 #2, W36a 闭环后可启动

---

## W36b — agent_review Web 入口 (UI 触发 review)

**关键经验:**

1. **写路径鉴权复用模式** — PROTECTED_PREFIXES 元组加一项
   - W34 是 `("/api/events",)`, W36b 加 `"/api/review"` 即可
   - 不需要在路由签名加 `Depends(require_user)`, 避开 FastAPI 422 解析坑 (W34 决策)
   - 模式可推广: 任何新写路径路由, 一行加入元组即获 401 拦截
   - 来源:`src/agent_swarm/web/app.py` `PROTECTED_PREFIXES`

2. **pr_ref 注入防御** — 显式黑名单 + shlex 校验
   - shell 危险字符: `;` `&` `|` `` ` `` `$` `>` `<` `\n` `\r` (覆盖 command substitution / pipe / redirect)
   - 双重保险: shlex.split 解析失败也拒
   - 原则: 信任用户输入是大忌, 即便 CLI 也是 — 防御前置, 不依赖下游 sanitize
   - 来源:`src/agent_swarm/web/routes.py` `_validate_pr_ref`

3. **agent_review 跨进程 env 切换** — sys.modules 清理模式
   - agent_review 在 import 时读 `AGENT_REVIEW_REPO` env 定位仓库 (W13 决策)
   - 设置 env 后必须 `sys.modules.pop("agent_review")` 清缓存, 否则常量已固定
   - finally 恢复 env + 再 pop, 保证幂等
   - 模式: 任何"配置依赖 env 一次性读取"的库都需要 sys.modules 清理
   - 来源:`src/agent_swarm/web/review_runner.py` 双重 env + sys.modules pop

4. **HTMX 表单模式** — 写表单 + 异步加载结果
   - `hx-post="/api/review" hx-target="#review-result" hx-swap="innerHTML"`
   - `hx-indicator` 显示 spinner, 完成后自动消失
   - 不需要 JS, 不需要 full reload — 完整 SPA 体验零 JS
   - 模式: 任何"提交后展示结果"的 UI 都能用, 避免 1 个 form 多个路由
   - 来源:`src/agent_swarm/web/templates/review.html`

5. **G-027 tmp git repo 测试模式** — 隔离 + 真实数据
   - 用 `tempfile.TemporaryDirectory` + `git init -b main` 构造隔离 repo
   - 真实 commit + 真实 diff, 比 mock 更可信
   - 4 cases (干净 / secret_leak / cmd_injection / schema) 一次过
   - 模式: 任何"依赖 git 状态"的功能测试都该用此模式
   - 来源:`tests/golden/test_g027_review_e2e.py` `_make_git_repo`

6. **薄包装层的价值** — review_runner.py
   - 不让 routes 直接 import `tools/agent_review` (边界 / 未来扩展点)
   - 集中处理: env 设置 / sys.modules 清理 / 异常分类 / cwd 切回
   - 未来 W36f 全模式 (LLM) 在 review_runner 加新函数, routes 不动
   - 模式: 跨工具集成的"中间层", 隔离工具特定逻辑 (env / IO / 异常)
   - 来源:`src/agent_swarm/web/review_runner.py`

**风险落地 (下轮关注):**

- 同步 review 阻塞 → W36b 接受, W36f 异步化
- 巨 PR 慢 → W36b 不限文件数, 加 timeout 留 W36f
- web_repo_root 配置遗漏 → 文档化, deployment guide 补 (W36b 已知)

**W36c 衔接:** vault://path#field URI 扩展 (W36a 留口子) 闭环 W36a 协议, 优先级 1

---

## W36c — vault://path#field URI 扩展 (闭环 W36a 协议)

**关键经验:**

1. **URI scheme 扩展模式** — 协议先行, 增量识别
   - 4 种 kind (literal / env / secret_ref / vault) 共用 parse_secret_ref 入口
   - 每加一种 kind, 在 `parse_secret_ref` 加一个 `if ref.startswith(...)` 分支
   - 不破坏老 kinds (W36a 22 case 不破)
   - 来源:`src/agent_swarm/web/auth.py` `parse_secret_ref` (W36a 3 kinds + W36c vault)

2. **SecretRef 扩展字段向后兼容** — default None 模式
   - 新增 `field: str | None = None` 字段, 老 3 kinds field 自动 None
   - frozen dataclass 保证不可变
   - `__post_init__` 校验时新字段可独立加检查
   - 模式: 扩展 dataclass 时所有新字段 default, 避免破坏老构造调用
   - 来源:`src/agent_swarm/web/auth.py` `SecretRef`

3. **JSON 文档 field 提取** — 协议层兜底
   - `vault://path#field` 调 `mgr.get(path)` 拿 Secret, 解析 JSON, 取 field
   - 失败路径: JSON 解析失败 → JWTError; field 缺失 → JWTError
   - 不污染 SecretManager ABC (不需加 `get_field` 方法, 通用接口保持干净)
   - JWTIssuer.resolve_secret 内部处理 JSON, 协议边界清晰
   - 来源:`src/agent_swarm/web/auth.py` `JWTIssuer.resolve_secret` 的 vault 块

4. **create_app vault 自动实例化** — 缺省行为
   - `vault://` 模式 + 无 `secret_manager` → 自动 `VaultSecretManager`
   - 需 `vault_url` / `vault_role_id` / `vault_secret_id` 关键字 (默认 127.0.0.1:8200)
   - 跟 W36a `secret://` 自动 EnvSecretManager 模式对称
   - 模式: 协议 + 缺省行为 = UX 友好 (用户少配), 测试时显式注入 fake
   - 来源:`src/agent_swarm/web/app.py` `create_app` 的 vault 分支

5. **W36a 3 kinds 兼容** — 字段 + 模式都兼容
   - SecretRef 老 3 kinds 构造完全不变, field 缺省 None
   - JWTConfig 老 3 kinds secret_ref 解析不变
   - W36a 22 个老 test case 全过 (跨 6 commit)
   - 模式: 协议扩展时, 始终先跑老测试, 确认零破坏再加新
   - 来源:`tests/unit/test_web_jwt_secret_ref.py` 老 case

6. **CLI `--web-jwt-secret-ref` 多协议支持** — 或运算
   - `if ref.startswith("secret://") or ref.startswith("vault://")` 一行覆盖
   - 不需新选项, 老 `--web-secret-manager vault` 复用
   - 模式: CLI 协议解析用 startswith 而非具体 protocol, 跟 parse_secret_ref 保持一致
   - 来源:`src/agent_swarm/cli/main.py` `if web_jwt_secret_ref and (...)`

7. **G-028 5 cases 一气呵成** — KV v2 JSON 文档模式
   - fake VaultSecretManager 模拟多 field 文档 (`{"current": ..., "previous": ...}`)
   - 4 case + 1 lifecycle: parse + resolve + encode + decode + rotate + 降级
   - 真值断言: 旧 token 在 cache TTL 内 verify, 触发 cache 后失效 (跟 G-026 套路一致)
   - 模式: 跨进程 / 跨组件协议, 用 multi-phase lifecycle 测试 SLA
   - 来源:`tests/golden/test_g028_vault_ref.py`

**风险落地 (下轮关注):**

- vault:// 仅支持 JSON → YAML 文档留 W36c+
- 自动 VaultSecretManager 实例化在测试场景不便 → 测试显式注入 fake (跟 W36a 一致)
- 多 worker SecretManager 各自一份 (W36a 限制)

**W36d 衔接:** 0.5.0a1 → 0.5.0a2 推进 (dist 重打, CHANGELOG 合并) — release 节奏

---

## W36d — 0.5.0a2 release 推进

**关键经验:**

1. **release 节点 = 汇总 + 引用** — 不重写 W detail
   - 0.5.0a2 节点结构: 汇总表 (7 slice + commit) + 各 W 段简述 (DoD/数据/已知限制)
   - 各 W 段不重复全文, 而是引用 commit hash + 关键 metric
   - 模式: 增量 release 节点作为"目录 + 摘要", 读者按需跳转到具体 W
   - 来源:`CHANGELOG.md` 0.5.0a2 节点

2. **version 三处同步** — pyproject / __init__ / app.py
   - grep 找出所有硬编码 `0.5.0a1` (3 处), 同步升级
   - 守门第 1 项自动 grep + 校验三处一致
   - 模式: 任何 version bump 先 grep `old_version` 找全部硬编码, 再批量替换
   - 来源:`tools/verify_w36d_dod.py` 守门 1

3. **dist 构建前置清理** — rm -rf dist/ build/ + *.egg-info
   - 老 build 残留会污染 (上次 commit 留了 0.5.0a1 + .bak 文件)
   - `python -m build` (PEP 517) 优于 setup.py (W27 模式)
   - 守门第 3 项验 dist/ 存在 0.5.0a2 sdist + wheel
   - 模式: 每次 build 前先清理, 保证 dist 干净
   - 来源:`tools/verify_w36d_dod.py` 守门 3

4. **twine check 不等于 twine upload** — 范围收口
   - W36d 只 `twine check` (验证元数据合法), 不 `twine upload` (需用户环境)
   - TestPyPI token + non-interactive terminal 是用户责任
   - 模式: CI/release 脚本只到 "dist ready" 为止, 上传人工触发
   - 来源:`tools/verify_w36d_dod.py` 守门 4 + 范围收口

5. **git tag 必新** — 0.5.0a2 是新 release
   - 老 tag 0.5.0 + 0.5.0a1 已存在 (0.5.0 是 W27 早期? 0.5.0a1 是 P5 启动)
   - 0.5.0a2 = 增量 release, 不覆盖老 tag
   - 守门第 5 项 `git tag` 列表中必须有 0.5.0a2
   - 模式: 每次 release 必先 `git tag` 看现有, 再决定 tag 名
   - 来源:`git tag` 输出 (0.5.0 / 0.5.0a1 / 0.5.0a2)

6. **CHANGELOG 段提取模式** — 守门第 8 项
   - 用 regex `## \[0\.5\.0a2\].*?(?=^## \[|\Z)` 提取 0.5.0a2 整段
   - 校验 7 个 slice (W33a/W33b/W34/W35/W36a/W36b/W36c) 都在段内
   - 模式: release 节点校验 "所有前置 slice 都被引用" — 防漏
   - 来源:`tools/verify_w36d_dod.py` 守门 8

**风险落地 (下轮关注):**

- TestPyPI 上传需用户环境 → D7 不调 upload, 状态保留
- 0.5.0a1 dist 残留 (.bak) → 清理后, dist 干净
- 多 release 标签 (0.5.0 + 0.5.0a1 + 0.5.0a2) → 守门第 5 项验全部存在

**W36e 衔接:** repo 级 `ruff format` 136 欠债 (W33a 已知) — 收尾

---

## W36 整阶段 PDCA 闭环 (4 slice × 4 阶段)

> commit 范围: `fff1823 → 259c6de` | 4 slice (W36a/b/c/d) | 32/32 DoD | 38 files +4113/-66

**关键经验 (5 条沉淀):**

1. **协议收口模式** — 同一需求走多 URI, 守门验"老 kinds 不破"
   - W36a 协议 (`secret://key` 自动 EnvSecretManager) + W36c 协议扩展 (`vault://path#field` VaultManager)
   - 1 协议 2 表达, 增量闭环不破老调用
   - 守门 #8 "W36a 3 kinds 仍工作" 是关键
   - 模式: 协议扩展 = 守门先验兼容, 再加新 URI 形式
   - 来源: `tools/verify_w36c_dod.py` 守门 8

2. **Web 入口渐进模式** — 简单版先跑通, 占位留升级口子
   - W36b agent_review Web 入口: `mode=full` 占位 → fallback simple (确定性 Judge)
   - W36f 计划: 升级 full mode (LLM + 异步任务) 不动 routes
   - 模式: 入口先有再优化, 渐进式升级, 不在初始版硬塞全功能
   - 来源: `src/agent_swarm/web/app.py` `/api/review` route + W36b 已知限制段

3. **release 节点模式** — 汇总表 + 引用, 不重写 W detail
   - 0.5.0a2 节点 = 7 slice 汇总表 + 各 W 段简述 (DoD/数据/已知限制)
   - 增量 release 节点作为"目录 + 摘要", 读者按需跳 W detail
   - 守门 8 项: version 一致 / CHANGELOG 节点 / dist / twine / tag / ruff+mypy / pytest / slice 引用
   - 模式: W36d 模式可复用 0.5.0a3 / 0.5.0 final
   - 来源: `tools/verify_w36d_dod.py` 全 8 项 + `CHANGELOG.md` 0.5.0a2 节点

4. **PDCA 自我闭环节奏** — 4 slice × 4 阶段 = 16 节点全过
   - 每 slice 1 commit (代码+测试+文档) + 1 守门脚本 (8 项) + 1 CHANGELOG 节点
   - P 阶段: W36x_PLAN.md (DoD + 风险 + 资源 + 守门点)
   - D 阶段: 实施 + 短状态 (TaskCreate in_progress → completed)
   - C 阶段: `tools/verify_w36x_dod.py` exit 0 + ruff 0 + mypy 0 + 全量 0 新失败
   - A 阶段: CHANGELOG 节点 + P5-RETRO 段 + MEMORY 段 + 1 commit 收口
   - 模式: 整阶段 4 个 A 段 commit 各 1 行, + 1 个整阶段 A 段 (W36 闭环)
   - 来源: W36a/b/c/d 4 个 A 段 commit hash (4761843/94bf26c/79c4067/259c6de)

5. **风险分级 + 范围收口** — release 守住 "dist ready" 边界
   - W36d 守 "twine check 不上传" 原则: 上传需用户环境 (TestPyPI token)
   - W36a-c 各 slice 守 "当前 slice 范围内" 原则: W36a 不动 vault (留 W36c), W36b 不动 LLM (留 W36f)
   - 模式: CI/release 脚本只到 "X 阶段产物" 为止, 越界动作人工触发
   - 来源: W36d §6 范围收口 + W36a/b 已知限制段

**W37 衔接 (PDCA 下一轮启动):**

- **W36f** (功能, 优先) — agent_review full mode (LLM + 对抗式) Web 异步入口
  - 闭环 W13 dogfooding 承诺, 不依赖用户环境
  - 工作量: 中等 (异步任务 + 流式进度 + LLM API 集成)
- **W36e** (技术债, 并行) — `ruff format` 148 文件欠债 (实测数, W36d 候选 136 已实测为 148)
  - 1-2h 原子 commit, 单独走, 不和功能 commit 混
  - 风险: 一次性大改动污染 blame, 建议 .ruff.toml 分批
- **W36g** (release, 阻塞) — 0.5.0 final
  - 需用户环境 TestPyPI 验证后才能打, 留 TODO
  - 衔接: W36d release 模式可复用

**P5 阶段累计 (W28 → W36 整段):**

- 测试: P5 守门 1342 passed (W36 阶段: 1204 → 1342)
- ruff 0 / mypy 0 全程
- 0.5.0a1 → 0.5.0a2 release 节奏成熟
- W28/W31/W32 GUI Web UI + W33a-W36d WebState 协议收口 = P5 中段闭环






---

## W36f — agent_review 异步入口 (LLM + SSE)

**关键经验 (6 条):**

1. **LLM judge 工厂模式** — 同一接口, 3 provider
   - `llm_judge_factory(provider: str)` 返 JudgeFn (async callable)
   - fake: 复用 W13 `_deterministic_judge` (零新依赖)
   - openai/anthropic: 占位 + API key fail-fast (真实 SDK 留 W37+)
   - 模式: 抽象协议 + 多实现, fake for test 是核心
   - 来源: `src/agent_swarm/web/review_runner.py` `llm_judge_factory`

2. **内存 task store + SSE 自实现** — 0 新依赖
   - `ReviewTask` dataclass (7 字段) + 全局 `_TASK_STORE: dict[str, ReviewTask]`
   - SSE 用 `asyncio.Queue` + `text/event-stream` 自实现, 不引 `sse-starlette`
   - 30s 心跳保活 (`: heartbeat\n\n`) 避免中间代理超时
   - 模式: 简单场景优先自实现, 第三方库留待真有需求时引入
   - 来源: `src/agent_swarm/web/review_runner.py` `subscribe_task` + `routes.py` `/events` 端点

3. **异步不阻塞 event loop** — `asyncio.to_thread` 模式
   - LLM 同步调用走 `asyncio.to_thread(_run_full_in_thread, ...)` 包装
   - 内部 `asyncio.run(_wrapper())` 让 thread 有独立 event loop
   - 测试用 `asyncio.wait_for` + 慢响应模拟验证不阻塞
   - 模式: sync 包 to_thread, async 包 wait_for, 双重保险
   - 来源: `src/agent_swarm/web/review_runner.py` `run_full_review_async` + `_run_full_in_thread`

4. **fake LLM 模式设计** — 端到端可测试不依赖 API key
   - W36f 阶段策略: fake 模式 = simple + 异步 (确定性 judge, 走 `run_simple_review`)
   - openai/anthropic 走 `run_full_review` (W13 占位) → 缺 key 抛 RuntimeError
   - 真实 OpenAI/Anthropic SDK + AdversarialVerifier.verify 留 W37+
   - 模式: fake for test + real for prod, 同接口不同实现
   - 来源: `src/agent_swarm/web/review_runner.py` `_run_full_in_thread`

5. **模式选择 (mode=simple/full) 兼容 W36b** — 零破坏
   - `--web-review-mode` 显式选 simple (W36b 同步) 或 full (W36f 异步)
   - 默认 full (W36f 主推), 但 W36b 测试/G-027 用 mode=simple 显式选同步
   - 模式: 入口统一, 模式分流, 测试可显式选
   - 来源: `src/agent_swarm/web/routes.py` `api_review` (统一入口) + W36b 测试 `_client(review_mode="simple")`

6. **风险分级 + 范围收口** — W36f 留 W37+ 3 个口子
   - 单进程内存 store → 多 worker 留 W37+ Redis
   - 真实 LLM SDK 接入 → 留 W37+ (模式抽好, 接入简单)
   - 任务清理后台 loop → 留 W37+ (cleanup_expired_tasks 函数已写, 后台调度未启)
   - 模式: 抽好接口 + 留好口子, 下轮渐进接入
   - 来源: `src/agent_swarm/web/review_runner.py` `cleanup_expired_tasks` + R2/R6/R8 风险登记

**W36e 衔接:** `ruff format` 148 文件欠债 (推荐接 W36f 后, 1-2h 原子 commit)

**W37 衔接 (W36f 留口子):** OpenAI/Anthropic SDK + AdversarialVerifier.verify 真实流程

---

## W36e — repo 级 `ruff format` 150 文件欠债清理

**关键经验 (3 条):**

1. **大文件批量格式化 = 1 原子 commit** — 不分批, 不和功能 commit 混
   - 150 files reformatted 一次落地, 标 "W36e: format only" 主题
   - 守门项 5 自动验"working tree 改动 = 150 files", 避免 commit 不完整
   - 模式: 纯技术债 commit 必须独立, 不夹带功能
   - 来源: `tools/verify_w36e_dod.py` 守门 5

2. **格式化不引入新错** — ruff check / mypy 双守门
   - ruff format 只调空白/引号/缩进 (PEP 8), 不改逻辑
   - 但保险起见: 跑完 format 必跑 ruff check + mypy 守门
   - 模式: 任何"代码风格变更"配双 lint 守门
   - 来源: `tools/verify_w36e_dod.py` 守门 2 + 3

3. **blame 隔离模式** — `.git-blame-ignore-revs` 隔离大 commit
   - 150 文件的格式化 commit 必然污染 blame (每行都动)
   - 解法: `.git-blame-ignore-revs` 记录该 commit hash, `git blame` 自动跳过
   - 模式: 大批量格式 commit 必须配 ignore-revs 配置 (W36e 留 TODO 完整配置)
   - 来源: W36e 已知限制

**数据:**

- 起点: 150 files would be reformatted (W33a 已知 136, W36a-f 累计 +14)
- 落地: 150 files reformatted, 35 left unchanged
- 守门: 5/5 全过
- 全量回归: 1238 passed (W36f 1233 + 5 修复)
- ruff check 0 / mypy 0 全程不破

**W36g 衔接 (阻塞):** 0.5.0 final, 等 TestPyPI 验证 (W36e 清理后 diff 干净, release 友好)

**W37 衔接 (W36f 留口子):** OpenAI/Anthropic SDK 真实接入 + `.git-blame-ignore-revs` 完整配置

---

## W36g — 0.5.0 final release (W36 阶段收口)

**关键经验 (5 条):**

1. **3 阶段 release 链节奏成熟** — 0.5.0a1 → 0.5.0a2 → 0.5.0
   - a1 (P5 启动) → a2 (W36 7 slice 汇总) → final (W36 6 slice 收口)
   - 每个阶段守门 8 项, 跨 7 commit 兼容 (W36a-f + 整阶段归档)
   - 模式: 增量 release 链, 每阶段 CHANGELOG 节点 + dist + tag
   - 来源: `tools/verify_w36{g,d}_dod.py` 8 项守门同结构

2. **CHANGELOG 节点不重写** — 0.5.0 final 引用 0.5.0a2
   - 0.5.0 节点结构: 汇总表 + 6 slice 简述 + 引用 0.5.0a2 节点
   - 不重写 0.5.0a2 的 detail, 让读者按需跳转
   - 模式: 节点层级化, 父节点引子节点
   - 来源: `CHANGELOG.md` 0.5.0 节点

3. **version 4 处同步** — 加 base.html 硬编码
   - pyproject / __init__.py / app.py / base.html 4 处
   - 守门项 1 用 regex 找全部硬编码并校验
   - 模式: 任何 version bump 先 grep `old_version` 找全部硬编码, 再 sed 批量替换
   - 来源: `tools/verify_w36g_dod.py` 守门 1

4. **老 tag force update 模式** — 0.5.0 早期误打覆盖
   - 老 0.5.0 tag 是 W27 早期误打 (指向 P5 收尾 commit)
   - W36g 0.5.0 final 是真实 release, 删老 tag + 重新打
   - 模式: force update 是 git 标签标准操作, 但要确认老 tag 含义
   - 来源: `git tag -d 0.5.0 && git tag 0.5.0` (W36g D8)

5. **TestPyPI 上传范围收口** — 0.5.0 留 TODO 等用户环境
   - W36g 只到 "dist ready" (sdist + wheel + twine check PASSED)
   - 上传需 `~/.pypirc` token + non-interactive terminal
   - 模式: CI/release 脚本只到产物 + 元数据校验, 不调 upload
   - 来源: `docs/P5-RETRO.md` 0.5.0 段 (D11 留 TODO)

**W36 整阶段累计 (W36a-f + 整阶段归档 + W36g):**

- 7+1+1 = 9 commit (W36a/b/c/d/e/f + 整阶段归档 + W36g release)
- 测试: 1204 → 1238 passed (+34, W36e +5 / W36f +18+5 / W36a/b/c 累计 +6)
- 守门脚本: 7 个 (verify_w36{a,b,c,d,e,f,g}_dod.py)
- Golden Case: 8 个 (G-022 ~ G-029)
- tag 序列: 0.5.0a1 → 0.5.0a2 → 0.5.0 (W36g 新增)
- dist: 0.5.0 sdist + wheel (W36g)

**W37 衔接 (5 候选, 优先级):**

1. W37 (LLM 真实接入) — OpenAI/Anthropic SDK + AdversarialVerifier.verify 真实流程 (W36f 留口子)
2. W37+ (`.git-blame-ignore-revs`) — W36e 150 文件 commit 隔离配置
3. W37+ (pyproject description) — Phase 2 → Phase 5 (W36g 留口子)
4. W37+ (Redis task store) — 多 worker 部署 (W36f 留口子)
5. W37+ (TestPyPI 上传) — 0.5.0 final 真实 release (需用户环境)

---

## W37 — 真实 LLM judge 接入 (OpenAI/Anthropic SDK + AdversarialVerifier)

**关键经验 (5 条):**

1. **judge_fn 工厂模式 + 协议层抽象** — 3 provider 同一接口
   - `_openai_judge_fn` / `_anthropic_judge_fn` / `_deterministic_judge` (fake)
   - 协议层: `(agent, hypothesis_id, round_no) -> Judgement`
   - 模式: Provider 抽好, fake for test + real for prod, 协议层不变
   - 来源: `tools/agent_review.py` `_openai_judge_fn` / `_anthropic_judge_fn`

2. **SDK response Union narrow** — 协议层兜底
   - OpenAI `resp.choices[0].message.content` (直接 str)
   - Anthropic `resp.content[0].text` — content 是 Union[TextBlock, ToolUseBlock, ...]
   - mypy 联合类型: 用 `hasattr(first, "text")` + `isinstance(..., str)` narrow
   - 模式: 任何第三方 SDK response 都要 narrow + try/except 兜底
   - 来源: `tools/agent_review.py` `_anthropic_judge_fn` content 处理

3. **JSON 解析失败 → UNCERTAIN 兜底** — DESIGN §6.2.5
   - LLM 返非 JSON / 字段缺失 → stance=UNCERTAIN, confidence=0.5
   - 兜底避免 AdversarialVerifier 协议层被破坏 (gather_round 会 UNCERTAIN 兜底)
   - 模式: 任何 LLM 解析失败不能 raise, 要 fallback 协议层 UNCERTAIN
   - 来源: `_openai_judge_fn` / `_anthropic_judge_fn` try/except 块

4. **测试模块级状态隔离** — autouse fixture 重置 sys.modules
   - `agent_review.REPO` 在 import 时固定 (env read once), 后续 env 改动无效
   - W37 测试需要每个 case 独立设 `AGENT_REVIEW_REPO`, 必须 `sys.modules.pop("agent_review")` 重置
   - 模式: 任何 module-level 读 env 的库, 测试都要重置 sys.modules
   - 来源: `tests/unit/test_agent_review_llm.py` `_reset_agent_review_module` fixture

5. **W13 占位 "fallback simple" 删除模式** — 真实流程替代占位
   - W13 `run_full_review` 是 stub, 缺 key 报错或 fallback simple
   - W37 删除 fallback, 真实调 `AdversarialVerifier.verify` + 3 judge
   - 模式: 占位功能在"真实流程 ready"时彻底删, 不保留"向后兼容"路径
   - 来源: `tools/agent_review.py` `run_full_review` 真实版

**W37 累计数据:**

- DoD 8/8 全过
- 测试增量: W36e 1238 → W37 1256 passed (+18: 14 LLM judge + 4 异步路径)
- 新增 judge_fn: openai / anthropic 真实 LLM 调用
- 0 新依赖 (openai>=1.40 / anthropic>=0.40 W1/W2 已装)
- 守门脚本: verify_w37_dod.py 8 项

**W37+ 衔接 (3 候选):**

1. W37+ (`.git-blame-ignore-revs`) — W36e 150 文件 commit 隔离
2. W37+ (pyproject description) — Phase 2 → Phase 5
3. W37+ (TestPyPI 上传) — 0.5.0 final 真实 release (需用户环境)

---

## W38 — Phase 5 收口 (.git-blame-ignore-revs + pyproject + RELEASE.md)

**关键经验 (4 条):**

1. **.git-blame-ignore-revs 隔离大规模 commit** — blame 跳过模式
   - 大规模格式化 commit (W36e 150 文件) 污染 git blame, 每行都显示该 commit
   - 解决方案: `.git-blame-ignore-revs` 记录需要跳过的 commit hash
   - 用户启用: `git config blame.ignoreRevsFile .git-blame-ignore-revs` (per-repo)
   - 模式: 任何大规模 commit (format / mass refactor) 必加 .git-blame-ignore-revs
   - 来源: `.git-blame-ignore-revs` + `README.md` "Git Blame Ignore" 段

2. **pyproject 元数据完整 = PyPI 友好** — description / keywords / classifiers
   - description: 单行清晰, 含版本阶段 (Phase 5) + 核心特性 (3-4 个)
   - keywords: 10+ 个覆盖核心功能, 利于 PyPI 搜索
   - classifiers: 必含 Python 版本 + License, 利于 PyPI 分类
   - 模式: release 阶段必有完整 PyPI metadata, 不是 release 后补
   - 来源: `pyproject.toml` description / keywords / classifiers

3. **RELEASE.md 入 git (不 docs/)** — 操作手册可发现性
   - `docs/` 在 .gitignore (untrack 设计文档)
   - 但 RELEASE.md 是发布操作手册, 应该是 git tracked (release 时必看)
   - 移到根目录 `RELEASE.md`, 与 README 同级
   - 模式: 用户操作手册放根目录或 docs-rendered/, 设计文档放 untrack docs/
   - 来源: `RELEASE.md` (从 `docs/RELEASE-0.5.0.md` 移到根目录)

4. **守门 6 项简化** — W38 纯配置/文档 slice
   - W36/W37 是功能 slice, 8 项守门
   - W38 是收口 slice, 6 项守门覆盖元数据 + W36/W37 baseline
   - 模式: 守门项数 = slice 复杂度, 不是越多越好
   - 来源: `tools/verify_w38_dod.py` 6 项守门

**W38 累计数据:**

- DoD 6/6 全过
- 0 新增代码, 0 新增测试 (纯配置 + 文档)
- pyproject 升级: description + keywords (5→13) + classifiers (0→9)
- 新增 .git-blame-ignore-revs + RELEASE.md + README 段
- 全量 1256 passed (W37 baseline 不破)
- ruff 0 / mypy 0 / format 0 欠债 (W37 留下的 1 欠债 format 顺手清)

**W39+ 衔接 (3 候选, 优先级):**

1. W39 (TestPyPI 真实上传) — `~/.pypirc` token 配后, 跑 `twine upload --repository testpypi`
2. W39+ (Phase 6 计划) — 多 worker / Redis / 1.0.0 准备
3. W39+ (用户 git config) — `.git-blame-ignore-revs` 全员启用

---

## W39 — Phase 6 启动 (PHASE6-PLAN.md)

**关键经验 (3 条):**

1. **开新 phase 模式 = "PLAN 文件 + W40 候选"** — 跟 W28 对称
   - `docs/PHASE<N>-PLAN.md` 写完整 (阶段背景 + 目标 + 范围 + DoD + 候选 + 风险)
   - 阶段启动 PLAN 是 1 个 slice, 1 个 commit 收口
   - 守门项: PLAN ≥500 字 + 含 4 关键词 + CHANGELOG 节点 + baseline 不破
   - 模式: 每个 phase 启动都走 "1 个 PLAN slice", 价值是开新方向 + 候选清单
   - 来源: `docs/PHASE6-PLAN.md` + `tools/verify_w39_dod.py` 5 项守门

2. **候选切片优先级 = 留口子排序** — Phase 6 启动第一个 slice
   - W40 优先 Redis task store (W36f 留口子, 多 worker 部署基础)
   - W41 多 worker (W33b 留口子)
   - W42 TestPyPI 上传 (W38 留口子)
   - W43 1.0.0 release 准备
   - 模式: 候选优先级 = 之前 slice 留口子, 不重新发明
   - 来源: `docs/PHASE6-PLAN.md` §3.1

3. **8-12 周弹性 + DoD 严格** — 范围灵活 / 收口严
   - 范围: 8-12 周 (W40-W50 灵活调整)
   - DoD: 1.0.0 final + 多 worker 部署 + 实战验证 + TestPyPI/PyPI 上传 (严格)
   - 模式: 范围留弹性, 收口严守门
   - 来源: `docs/PHASE6-PLAN.md` §3 + §4

**W39 累计数据:**

- DoD 5/5 全过
- 0 新增代码, 0 新增测试 (Phase 6 启动 PLAN slice, 跟 W28 对称)
- PHASE6-PLAN.md 2596 字
- CHANGELOG W39 节点 (Phase 6 启动)
- 守门脚本 verify_w39_dod.py 5 项

**W40+ 衔接 (Phase 6 候选):**

- W40: Redis task store 真实接入 (W36f 留口子, Phase 6 第一个具体 slice)
- W41: 真实多 worker 部署 (W33b 留口子, 依赖 W40)
- W42: TestPyPI 真实上传 (W38 留口子, 需用户 `~/.pypirc` token)
- W43: 1.0.0 release 准备
- W44+: 实战验证 + 用户反馈循环

**Phase 5 整段最终状态:** ✅ 闭环 (W28-W38 累计, 0.5.0 final production-ready)
**Phase 6 启动状态:** ✅ PLAN 落地, W40 候选明确

---

## W40 — Redis task store 真实接入 (TaskStore Protocol + Memory/Redis 双实现)

**关键经验 (5 条):**

1. **TaskStore Protocol 抽象接口** — 跟 W33b WebStateStore 对称
   - 5 方法 (async): create_task / get_task / update_task / subscribe_task / cleanup_expired
   - Protocol 模式: 同接口多实现, 工厂函数选 backend
   - 模式: Phase 3 (W33b) → Phase 5 (W40) 复用 store protocol 模式
   - 来源: `src/agent_swarm/web/review_runner.py` `TaskStore(Protocol)`

2. **MemoryTaskStore 包装现有 = W36f 行为零破坏** — async 包装
   - 现有 W36f `_TASK_STORE` / `_TASK_QUEUES` 模块级 dict 不变
   - 5 方法改为 async 包装 sync 实现, 匹配 Protocol
   - 模式: 抽象时先包装现有, 再加新 backend, 零回归
   - 来源: `MemoryTaskStore` 5 个 `async def` 方法

3. **RedisTaskStore = hash + sorted set + pub/sub 三件套**
   - hash `task:{task_id}` 存 task 字段 (status / progress / log / result / error)
   - sorted set `tasks:pending` 存待清理 task_id (按 created_at 排序)
   - pub/sub `task:{task_id}:events` 推 SSE 事件 (跨 worker 通知)
   - 模式: Redis 不止是 cache, 是 publish-subscribe 平台
   - 来源: `RedisTaskStore.create_task` / `update_task` / `subscribe_task`

4. **DSN 缺省降级零破坏 = W33b 模式复用** — redis 缺 → 降级 memory
   - `create_task_store(backend, redis_dsn)` 检查
   - `redis` + 无 DSN → 警告 + 自动降级 MemoryTaskStore
   - `redis` + 缺 redis 包 → 同上
   - 模式: 任何新 backend 都配 DSN 缺省降级路径
   - 来源: `create_task_store` 工厂

5. **Protocol 抽象 + 工厂 + CLI + 测试模式** — 完整生态
   - Protocol (抽象) + MemoryTaskStore/RedisTaskStore (实现) + create_task_store (工厂) + --web-task-store (CLI) + test_web_review_task_store.py (测试) + verify_w40_dod.py (守门)
   - 6 组件齐全, 跟 W33b PG store 模式对称
   - 模式: 抽象接口 + 多实现 + 工厂 + CLI 暴露 + 守门
   - 来源: W40 6 文件齐全, 14 case, 8 项守门

**W40 累计数据:**

- DoD 8/8 全过
- 测试增量: W39 1256 → W40 1270 passed (+14)
- 新增 TaskStore Protocol + 2 实现 + 工厂
- 0 新依赖 (redis>=5.0.0 W18 已装, fakeredis>=2.20.0 dev 已装)
- 守门脚本: verify_w40_dod.py 8 项

**W41+ 衔接 (Phase 6 候选):**

1. W41 (多 worker 部署实战) — gunicorn/uvicorn workers (W40 依赖, 多 worker 共享 task store)
2. W42 (TestPyPI 真实上传) — 用户环境 `~/.pypirc` token
3. W43 (1.0.0 release 准备) — version 升级 + CHANGELOG final
4. W44+ (实战验证) — 用户反馈循环

## W41 (2026-06-24): 多 worker 部署实战

**闭环**: 8/8 守门过, 1368 passed

**关键经验**:
1. **W40 闭环缺口** — routes.py 完全没用 `app.state.task_store`, 继续走模块级 `_rr.create_task/get_task/subscribe_task`。W40 闭环只验证了 Protocol + Memory + Redis 实现,没验证 routes 走 store。**新写"接入"代码必须 e2e 走真实 HTTP 路径,不要只验 Protocol 方法**。否则跨 worker 状态永远不可见,但单 worker 路径全跑通你发现不了。
2. **uvicorn factory 模式** — `workers=N` fork 子进程, 子进程要无参 import-string factory; 配置从 env 读,不能传 kwargs。`os.environ` 注入要在 `uvicorn.run` 之前; `uvicorn.run` 是同步阻塞,跟现有 asyncio loop 冲突, 多 worker 模式只起 web 跳 swarm 主流程 (YAGNI)。
3. **RedisTaskStore 跨实例共享** — `from_redis_client(client)` 比 `from_url(dsn)` 更适合 e2e: fakeredis 传同一 client 共享状态, 不用启真 TCP server。但 client 必须 `decode_responses=True` 跟 `from_url(decode_responses=True)` 对齐, 否则 hgetall 返 bytes keys, get_task KeyError。
4. **跨 worker SSE 通知** — Redis pub/sub channel `task:{id}:events` 是 key, B 先 subscribe 再 A publish, 收到事件; subscribe 到 done 事件后流自动关闭 (status in done/error → break)。
5. **cleanup_expired 幂等** — 跨 worker 各自跑 cleanup, 第二个 worker 看到 0 (已被第一个删了), 自然幂等。不需要分布式 lock。**但 sorted set `tasks:pending` 的 score 和 hash 里的 `created_at` 都得过 TTL 测试, 改一个不够** (e2e 调过这个坑)。
6. **smoke 脚本 vs e2e 测试** — smoke 跑真 subprocess 启 uvicorn (跨进程), 验证 CLI 启动 + 干净退出; e2e 走单进程多 app 实例 + httpx ASGITransport, 验证业务逻辑 (跨实例共享 fakeredis)。两者覆盖不同维度, 都必须有。

**链接**: [[stage-gate-on-dod]] / [[self-driven-execution]] / [[w33-pg-store-pattern]] (Protocol + 降级零破坏模式)

## W43 (2026-06-25): 1.0.0-rc1 release 准备

**闭环**: 8/8 守门过, 1368 passed, version 0.5.0 → 1.0.0-rc1

**关键经验**:
1. **TUI drain 模式** — `_pump_events` 队列非空时用 `get_nowait` 一次拉多个事件, max_drain=1000 兜底防死循环。比 `await get()` + wait_for 0.5s 阻塞快很多,大场景 (100 task 涌入) 显著加速。Windows 实际效果需 W44 实战验证 (Linux 没法测 asyncio 调度差异)。
2. **version bump 三处同步** — pyproject.toml / __init__.py / app.py 默认参数。`re.search` 一处抓,守门脚本三处对比断言,简单可靠。
3. **dist 重建 + twine check** — `python -m build` → sdist + wheel, `twine check` PASSED 即可发布,无需外部验证。`dist/agent_swarm-1.0.0rc1-{py3-none-any.whl,tar.gz}` 各 245K / 503K。
4. **git tag 本地不 push** — 按 [[local-commit-no-push]] 规则,tag 跟 commit 都不主动推 origin。`git push --follow-tags` 默认行为是推 commit + tag, 但要等用户明确确认。
5. **W43b 阻塞处理** — 跟 W42 TestPyPI 同样的阻塞模式(需用户 token), 在 PLAN 显式标"阻塞", 在 CHANGELOG 已知限制段写明"等用户", 在 W43 闭环里把 W43b 标 "不在本 slice"。诚实优于假装闭环。
6. **W42 守门脚本欠债** — Tony 写的 verify_w42_dod.py 有 SIM102 嵌套 if, W43 守门跑过才暴露,顺手合并了。一行 `if a and b:` 替代嵌套两层, ruff SIM102 修。

**链接**: [[stage-gate-on-dod]] / [[local-commit-no-push]] / [[self-driven-execution]] / [[w33-pg-store-pattern]]
