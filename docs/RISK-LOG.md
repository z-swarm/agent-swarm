# agent-swarm 风险登记表

> DESIGN §17.6 风险登记表 review 落地。每月 review 一次，新增/淘汰由 PR 评审决定。
> 上次 review：2026-06-20 (W15)

## 当前风险（按 P×I 排序）

| ID | 风险 | 概率 | 影响 | 早期信号 | 应对 | 状态 | 闭环周次 |
|----|------|------|------|---------|------|------|----------|
| R01 | MCP 协议 spec 变更 | 中 | 中 | Anthropic blog / changelog | 锁定 SDK 版本，每月 review；适配层隔离 | 🟢 已应对 | — |
| R02 | 飞书 API 改版 | 中 | 中 | Lark 开发者通知 | LarkConnector 单独可替换；卡片模板版本化 | 🟢 已应对 | — |
| R03 | LLM provider 限流突变 | 高 | 高 | nightly 失败率上升 | 每个 provider 独立 circuit breaker；多 provider failover (G-010) | 🟢 已应对 | — |
| R04 | AdversarialVerifier 不收敛 | 中 | 高 | rounds_used 经常 ≥4 | max_rounds 强制截断；Golden Case G-013 监控；§16.3 #9 调参 | 🟡 监控中 | W21 复审 |
| R05 | SQLite WAL 在容器/NFS 不可靠 | 高 | 中 | flock 警告日志 | §10.2 已警告；生产强制 Redis (W18) | 🟡 应对中 | W18 |
| R06 | Agent 死循环烧 token | 中 | 高 | tokens_used 突增 | TokenBudget 硬上限 + max_tokens_per_task；超限自动 stop | 🟢 已应对 | — |
| R07 | Prompt injection 越权 | 高 | 高 | security.policy_check denied 事件 | SecurityPolicy 黑白名单 + Sandbox + Approval；G-006/G-007 持续验证 | 🟢 已应对 | — |
| R08 | LLM 输出 schema 漂移 | 中 | 中 | structured output 解析失败率 | Provider 层 retry + JSON Schema 强制约束；fallback 到自由文本 + 解析 | 🟢 已应对 | — |
| R09 | 测试 LLM 真实调用成本失控 | 中 | 中 | nightly cost > $X | Golden Case 用 cheap model（gpt-4o-mini）跑大部分；旗舰模型只跑 P1 case | 🟢 已应对 | W15 加 Prometheus 成本指标 |
| R10 | TUI 在 Windows 终端兼容性 | 高 | 低 | Issue 反馈 | Textual 跨平台测试；fallback 到非 TUI mode；W14b 起 windows-latest runner 烟测 | 🟡 缓解中 | W14b 落地 |
| R11 | Golden Case 漂移（manual expected.yaml 失同步） | 中 | 中 | review 时发现 must_find 不命中 | 每月 review；新增 case 必须 PR 评审；case 列表版本控制 | 🟢 已应对 | — |
| R12 | MCP server 持续崩溃（>5min） | 中 | 中 | mcp.circuit_state gauge 持续 = 1 | W14a 熔断 60s + 半开探活；doctor --mcp-config 主动健康检查 | 🟢 已应对 | W14a |
| R13 | 多租户越权（Phase 3） | 中 | 高 | security.cross_tenant_attempt 事件 | W16-17 50 条攻击套件 + 100 并发压测 0 越权 | ⚪ 待应对 | W17 |
| R14 | Docker SDK 在 Windows 兼容性 | 高 | 中 | W19 期间 windows runner flaky | W19 保守化：WORKSPACE_ONLY 默认 + DOCKER opt-in；Linux 优先 | 🟡 应对中 | W19 |
| R15 | Vault 凭证轮换窗口数据丢失 | 低 | 高 | secret.rotation_due 事件 | 5 分钟内生效（agent 无感）；监控 + 飞书通知 | ⚪ 待应对 | W20 |
| R16 | 测试 LLM 真实调用在 CI 限流 | 中 | 中 | CI 失败率上升 | CI 全部 mock；nightly 单独跑真实 LLM；fast-checks 守门 | 🟢 已应对 | — |
| R17 | 第三方 secret 在代码提交 | 中 | 高 | agent_review.py 静态扫描触发 | W13 7 类规则（含 secret_leak）；W15 --require-human-review 守门 | 🟢 已应对 | W15 |
| R18 | 性能 baseline 漂移（夜间跑） | 中 | 中 | benchmark 报警 | §17.5 报警规则：+20% 警告 / +50% 阻塞 | 🟢 已应对 | — |
| R19 | observability sink 异常拖慢业务 | 中 | 中 | sink.consume 抛异常 | bus.emit_event 内部 try/except 兜底；sink 自身也 catch | 🟢 已应对 | — |
| R20 | 并发安全（同一 task 多 agent 抢） | 中 | 高 | CAS version_mismatch 指标突增 | TaskQueue CAS 乐观锁；§6.4.2 设计 | 🟢 已应对 | — |

## 已淘汰风险（历史）

| ID | 风险 | 淘汰原因 | 淘汰日期 |
|----|------|---------|----------|
| R00 | 旧版 W5 sandbox 工具白名单可绕过 | 升级到 P0-1 真防护（shell=False + symlink/TOCTOU/EBADF 修复） | 2026-06-15 |

## 风险颜色图例

- 🟢 已应对：监控 + 应对方案都到位
- 🟡 监控中 / 应对中：监控到位但应对方案在落地中
- ⚪ 待应对：风险已识别，应对排进路线图

## 评审记录

| 日期 | 评审人 | 变更 |
|------|--------|------|
| 2026-06-20 | Mavis (W15 起草) | 初始化 20 条风险；R10 升级为 🟡（W14b windows runner 缓解）；R12/R17 标记 🟢（W14a/W15 闭环） |
| TBD | TBD | 每月 review；新增/淘汰由 PR 评审决定 |

## 相关文档

- DESIGN §17.6 风险登记表
- DESIGN §16 开放问题
- `docs/REVIEW-2026-06-19.md` + `REVIEW-2026-06-19-2.md`：审查记录
- `docs/PHASE3-PLAN-2026-06-20.md`：阶段计划（哪些 W 闭环哪些风险）
