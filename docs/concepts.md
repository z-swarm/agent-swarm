# agent-swarm 核心概念

> DESIGN §1 + §6 + §17.7 配套文档。每章 ≤300 字 + 1 张图。

---

## 1. Agent（代理）

agent-swarm 的基本执行单元。每个 Agent 拥有：

- `id` / `role` / `persona`（个性化提示）
- `model` + `provider`（openai / anthropic）
- `AgentCapabilities`（allowed_tools + 是否能 spawn / assign / execute）
- `tools` / `skills` 列表

Agent **不直接包含行为**——行为由 `AgentRunner` 驱动。Agent 本身是纯数据 + 配置，便于持久化/序列化。

```
┌──────────────────────────────────────────────┐
│ Agent (dataclass)                            │
│   id: "reviewer"                             │
│   role: "code-reviewer"                      │
│   model: "gpt-4o-mini"                       │
│   capabilities: plan_only()  ← 不能执行工具  │
│   tools: ["read_file", "search_code"]        │
└──────────────────────────────────────────────┘
                ↓ AgentRunner 驱动
        observe → think → act → reflect
```

**三种预设**：
- `worker()` — 可执行工具（默认）
- `lead()` — 只能 spawn / assign / review
- `plan_only()` — 只能读，不能执行

---

## 2. Task（任务）

工作流的最小工作单元。`TaskQueue` 维护任务状态：

- 状态机：`pending` → `blocked` → `in_progress` → `completed` / `failed`
- CAS 乐观锁：`version` 字段保证并发安全
- `depends_on` 声明依赖（被依赖任务未完成 → 自动 blocked）

`claim()` 是核心 API——返回 `ClaimResult` 含 `success` + `reason` 字段（`task_not_found` / `version_mismatch` / `already_claimed` / `dependency_blocked` / `task_terminal`）。

```
   pending ──claim()──> in_progress ──complete()──> completed
      │                     │                             
      │                     └── fail() ──> failed          
      │                                                  
      └── has depends? ──> blocked                       
```

**并发安全**：多个 agent 抢同一任务时，version 不匹配者得到 `version_mismatch` reason，需重读 task 后重试。

---

## 3. Mailbox（消息）

Agent 间点对点通信通道。W2 起内存实现，W3 起持久化（与 `SessionEvent` 共用 SQLite store）。

- `Message.id` 唯一标识
- `from_agent` / `to_agent`（None = broadcast，W2 不实现）
- `msg_type`: `question` / `challenge` / `reply` / `notify` / `delegate`
- `reply_to` 字段支持线程
- `refs` 列表引用其他消息/任务

```
Agent A ──send("review this")──> Mailbox ──> Agent B
   │                                │
   │                                ↓
   │                          ┌──────────────┐
   │                          │ B 的 inbox   │
   │                          │ (FIFO 读取)  │
   │                          └──────────────┘
   │
   └──<─reply("LGTM")─────── B 处理完 ──>──┘
```

**何时用 Mailbox vs TaskQueue？**
- TaskQueue：需要 CAS 抢的执行单元
- Mailbox：通知 / 提问 / 委派等"软"通信

---

## 4. KnowledgeBase（知识库）

W4 启用——agent 经验的持久化层。存储"已学到的经验"，支持按关键词检索。

- 写入：`KB.put(key, value, ttl=...)`
- 读取：`KB.get(key)` / `KB.search(query, k=5)`
- TTL：自动过期（防 stale knowledge 污染）

**与 ConversationContext 隔离**——KB 是"组织记忆"，`ConversationContext.history` 是"个人对话"。两者**不互通**。

```
   ┌──────────┐
   │ Agent A  │  put("G-001 命中", value, ttl=30d)
   └─────┬────┘
         ↓
   ┌──────────┐
   │    KB    │  key → value,  索引: keyword, recency
   └─────┬────┘
         ↓
   ┌──────────┐
   │ Agent B  │  search("G-001") → cached value (60% 命中)
   └──────────┘
```

**应用**：G-001 PR 安全审查，第二次跑同一 case 时直接读 KB 缓存，跳过 LLM 调用，**节省 60% token**（§17.2 W4 DoD）。

---

## 5. AdversarialVerifier（对抗式验证）

Phase 2 引入——多 agent 互相质疑、迭代收敛的根因定位机制。

- 每个 `judge agent`（plan_only）对每个 `hypothesis` 给出 `Judgement`（stance + confidence + evidence）
- 多轮迭代：每轮 agent 看到他人观点（含证据），允许立场更新
- 收敛条件（按优先级）：
  1. `len(survivors) <= min_survivors`
  2. 连续 2 轮无淘汰 + 无立场变化
  3. `round_no >= max_rounds`
  4. 全部淘汰兜底

```
Round 1:  3 judge × 5 hypothesis = 15 个 Judgement
  ↓ 评分 + 淘汰
Round 2:  2 judge × 4 hypothesis = 8 个 Judgement
  ↓ 评分 + 淘汰
Round 3:  2 judge × 2 hypothesis → min_survivors=1 触发 → 收敛
  ↓
Verdict: survivors=[H2], root_cause=H2.statement, confidence=0.85
```

**为何不用单轮投票？** 单轮投票无证据交换——A 不知道 B 看到了哪些 log。AdversarialVerifier 多轮迭代 + 证据共享，是真正"独立判断后讨论"。

**Golden Case G-011~G-015**：5 个调试场景，根因命中率 100% ≥ 80% DoD。

---

## 附录：模块索引

| 概念 | 模块 | 入口 |
|------|------|------|
| Agent | `agent_swarm.core.types` | `Agent` dataclass |
| Task | `agent_swarm.core.task_queue` | `TaskQueue.claim/complete/fail` |
| Mailbox | `agent_swarm.core.mailbox` | `Mailbox.send/receive` |
| KB | `agent_swarm.core.knowledge_base` | `KnowledgeBase.put/get/search` |
| AdversarialVerifier | `agent_swarm.core.adversarial` | `AdversarialVerifier.verify` |
