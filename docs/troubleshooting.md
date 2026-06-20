# agent-swarm 故障排查

> DESIGN §17.7 DX 工具配套文档。≥10 个常见错误 + Golden Case 链接 + 排查步骤。

## 1. "agent-swarm run" 启动失败

| 症状 | 排查 | 修复 |
|------|------|------|
| `Failed to load config: ...` | YAML 语法错（缩进、引号） | `python -c "import yaml; yaml.safe_load(open('config.yaml'))"` |
| `command not found`（sandbox 模式） | Windows + bash 路径问题 | 用绝对路径或 `workspace_only` 模式 |
| `session db parent directory does not exist` | `~/.agent_swarm/` 不存在 | `mkdir -p ~/.agent_swarm` |
| `session db is not writable` | 多用户共享机器权限问题 | `chmod 700 ~/.agent_swarm` |

**快速诊断**：`agent-swarm doctor`（W14b 起）— 4 类检查 1 步出结果。

---

## 2. LLM 调用失败

| 症状 | 排查 | 修复 |
|------|------|------|
| `openai.AuthenticationError: 401` | `OPENAI_API_KEY` 失效或未设 | `export OPENAI_API_KEY=sk-...` |
| `anthropic.PermissionDeniedError: 401` | `ANTHROPIC_API_KEY` 未设 | `export ANTHROPIC_API_KEY=sk-ant-...` |
| `RateLimitError: 429`（连续 3 次） | LLM provider 限流 | 触发 G-010 自动 failover（OpenAI→Anthropic） |
| `JSON schema 解析失败` | LLM 输出格式漂移 | 启用 provider 层 retry + JSON Schema 强制（§7.2） |

**Golden Case 对应**：
- G-006: prompt injection `/etc/passwd` → SecurityPolicy 黑名单拦截
- G-007: 越权读 `~/.ssh/authorized_keys` → 路径黑名单 + sandbox workspace_only
- G-010: OpenAI 限流 → 自动切 Anthropic 成功

---

## 3. MCP server 问题

| 症状 | 排查 | 修复 |
|------|------|------|
| `MCPConnectionError: command not found` | stdio command 路径错 | 用绝对路径或保证 PATH 含该 binary |
| `MCP server crashed; reconnect 3 times; circuit OPEN` | server 真实崩溃 | 等 60s cool_off 后重试；或 `kill` 后重启 server |
| `MCPCircuitOpenError` 调用立即拒绝 | circuit breaker OPEN | 检查 `agent-swarm doctor --mcp-config ...`；等 cool_off |
| `SSE 4xx/5xx` 错误 | server URL 错或鉴权失败 | 验证 token / URL；W14a 起 SseMCPClient 4xx 抛 MCPHTTPError |
| `bearer token 不对` | `MCPServerConfig.auth=bearer` 缺 token | YAML 配 `token: "${MCP_DB_TOKEN}"`（强制走 SecretManager） |

**Golden Case G-018**：MCP server 崩溃 → 3 次重连失败 → 熔断 → 快速拒绝 — 端到端跑通。

**自检工具**：
```bash
agent-swarm doctor --mcp-config examples/w14a_mcp_resilience.yaml
python tools/count_reconnect.py   # 验证脚本（无需 LLM）
```

---

## 4. Sandbox 拦截

| 症状 | 排查 | 修复 |
|------|------|------|
| `policy denied: sensitive path blocked` | 访问 `/etc/passwd` / `~/.ssh/...` | 改用非敏感路径；或 `--allow-path`（不推荐） |
| `command injection: ';' not allowed` | 命令含 shell 元字符 | 用 `args=[...]` 形式而非 shell 字符串 |
| `path traversal: '..' detected` | 路径含 `..` | 绝对化路径 |
| `token 超限截断` | 单次工具返回 > token 预算 | 调大 `--max-tokens` 或在代码里 chunk 输出 |

**Golden Case G-006/G-007**：20 条攻击套件验证拦截。

---

## 5. Session 恢复问题

| 症状 | 排查 | 修复 |
|------|------|------|
| `Session not found` | `session_id` 输错 | `agent-swarm session list` 看真 ID |
| `Session database not found` | 默认 db 路径不存在 | `agent-swarm run` 跑过一次会自动建 |
| 恢复后 task 描述为空 | 旧 event 缺 `description` 字段 | 升级到 W3+ 重新跑（P2-11 fix） |
| `events=0` 但 session 有数据 | db 路径不一致 | 检查 `--db` 参数；用 `session list` 找对 db |

---

## 6. 性能/Token 问题

| 症状 | 排查 | 修复 |
|------|------|------|
| 任务跑超 5 分钟 | LLM 调用慢 | 切更小模型（gpt-4o-mini）；启用 KB 缓存 |
| Token 烧光（> 预算 95%） | `framework_llm_tokens_total` 告警 | TokenBudget 硬限 + W15 Prometheus 埋点 |
| 重复 case 慢 | 没用 KB 缓存 | 确认 KB 路径一致；G-001 case 二跑应 < 60% token |
| `Agent 死循环烧 token` | `max_iterations` 未生效 | AgentRunner 强制 stop + 报错（§17.6） |

---

## 7. 飞书连接器问题

| 症状 | 排查 | 修复 |
|------|------|------|
| `Lark signature verification failed` | 签名错（最常见是 encrypt_key 启用但解密路径走错） | 确认 `LARK_ENCRYPT_KEY` 走 AES-256-CBC（REVIEW-2026-06-19-2 L1） |
| `event not received` | webhook URL 未注册 | 飞书开发者后台 → 事件订阅 → 填入 `<your-host>/lark/webhook` |
| `card interaction timeout` | `ChannelApprover` fail-closed | 用户超时默认拒绝；按需调整 `per_round_timeout` |

**Golden Case G-015~G-017**：飞书真实工作区交互。

---

## 8. 通用排查流程

```bash
# 1) 跑 doctor 一键检查
agent-swarm doctor --skip-llm

# 2) 跑对应 Golden Case
pytest tests/golden/test_golden_p2.py -v -k g011

# 3) 看 session 事件流
agent-swarm session list
agent-swarm session show <id> --events

# 4) 跑 W6-W13 验收脚本
python tools/verify_w10_dod.py
python tools/verify_w11_dod.py
python tools/verify_w12_dod.py
python tools/verify_w13_dod.py

# 5) 跑 Dogfooding 自审
python tools/agent_review.py --mode=simple
```

---

## 9. 已知限制

| 限制 | 说明 | 何时修 |
|------|------|--------|
| TUI 在 Windows 终端兼容性 | Textual 跨平台偶发色彩丢失 | W14b 加 windows-latest runner 跑烟测 |
| SQLite WAL 在 NFS 不可靠 | 多进程并发写可能丢锁 | Phase 3 W18 Redis 后端 |
| MCP 协议 spec 变更 | SDK 版本绑定 | §17.6 风险表 — 每月 review |
| AdversarialVerifier 不收敛 | 极端 case `rounds_used ≥ 4` | G-013 监控；§16.3 #9 调参 |

---

## 10. 提 Issue / 找帮助

- 已知问题：`docs/RISK-LOG.md`（W15 引入）
- 审查记录：`docs/REVIEW-*.md`
- 设计文档：`DESIGN.md` §16 开放问题
- Phase 3 计划：`docs/PHASE3-PLAN-2026-06-20.md`

---

## 附录：Golden Case 索引

| Case | 类别 | 用途 |
|------|------|------|
| G-001 | Code Review | PR 安全审查 |
| G-002~G-010 | P1 | Phase 1 端到端验收（10 个） |
| G-011~G-015 | P2 | AdversarialVerifier 根因定位（5 个） |
| G-016 | Approval | 高风险命令需要审批 |
| G-017 | MCP | GitHub MCP 创建 issue |
| G-018 | MCP | Server 崩溃重连 + 熔断 |
| G-019 | Multi-tenant | 100 并发跨租户 0 越权 |
| G-020 | Scale | 10 agent + Redis 后端 |

跑特定 case：`pytest tests/golden/ -k G-018` 或 `pytest tests/golden/test_golden_p2.py -k g011`。
