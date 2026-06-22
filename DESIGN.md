# agent-swarm 架构设计文档 v4.2

> **状态**: 待评审 | **日期**: 2026-06-16 | **版本**: v4.2（工程化交付版）

## v4.2 修订摘要

v4.1 解决了"做什么"，v4.2 解决"如何确保做对、做完、能跑"。新增 §17 工程实践与交付门禁，确保产出**可运行、可验证、可演进**的项目。

| # | 新增内容 | 解决的问题 |
|---|---------|---------|
| 1 | §17.1 垂直切片 MVP | 把 Phase 1 重排为 6 周 Weekly Slice，每周末有可演示产物——避免"99% 完成永远跑不起来"陷阱 |
| 2 | §17.2 Definition of Done | 每个 Phase 给出量化的完成标准，避免主观判断 |
| 3 | §17.3 验收场景库（Golden Cases） | 维护 20 个真实案例 + 期望结果，作为 e2e 测试基线 |
| 4 | §17.4 测试金字塔 + CI 门禁 | 分层测试策略、LLM mock、覆盖率门槛、PR 阻塞规则 |
| 5 | §17.5 性能与质量基线 | 周度 benchmark + 劣化报警 |

> v4 / v4.1 修订摘要见 [附录 B](#附录-bv4-修订摘要)。

---

## 目录

1. [项目定位](#1-项目定位)
2. [整体架构](#2-整体架构)
3. [交互层设计](#3-交互层设计)
4. [消息通道层设计](#4-消息通道层设计)
5. [可观测性（横切面）](#5-可观测性横切面)
6. [编排与核心模块](#6-编排与核心模块)
7. [Agent Runtime](#7-agent-runtime)
8. [安全模型](#8-安全模型)
9. [LLM Provider 层](#9-llm-provider-层)
10. [存储后端](#10-存储后端)
11. [内置技能系统](#11-内置技能系统)
12. [高性能策略](#12-高性能策略)
13. [项目目录结构](#13-项目目录结构)
14. [使用示例](#14-使用示例)
15. [MVP 分阶段计划](#15-mvp-分阶段计划)
16. [开放问题](#16-开放问题)
17. [工程实践与交付门禁](#17-工程实践与交付门禁) ★ v4.2 新增
18. [附录 A：核心数据类型字典](#附录-a核心数据类型字典)
19. [附录 B：v4 修订摘要](#附录-bv4-修订摘要)

---

## 1. 项目定位

独立的通用多 Agent 协作框架。不绑定特定 LLM 平台，提供完整的 agent swarm 协调层。

### 核心理念

| 概念 | 说明 |
|------|------|
| **去中心化协调** | Task Queue + 乐观锁 CAS 让 agent 自己认领任务，无需中央调度器 |
| **点对点通信** | Mailbox 实现 agent 间直通，Team Lead 只编排不动手 |
| **对抗式验证** | 多 agent 从不同假设出发互相质疑，在交叉验证中逼出真相 |
| **委托模式** | 协调者与执行者分离，Lead 只编排，不动手 |
| **独立上下文 + 共享知识** | 对话历史隔离保证独立判断，共享项目知识保证一致性 |
| **默认安全** | 高风险操作默认审批，API 默认认证，敏感路径默认禁止 |

### 差异化

| 对比维度 | Subagents (Claude Code) | Agent Teams (Claude Code) | agent-swarm |
|---------|------------------------|---------------------------|-------------|
| 通信拓扑 | 星型（主从汇报） | Mesh + 共享状态 | Mesh + 共享状态 |
| 会话恢复 | ✓ 支持 | ✗ 不支持 | ✓ 支持 |
| 持久化记忆 | ✗ | ✗ | ✓ 支持 |
| LLM 绑定 | Claude Only | Claude Only | 多 Provider |
| 可观测性 | 终端输出 | 终端输出 | Metrics + Dashboard |
| 交互方式 | CLI | CLI | CLI + SDK + IM |
| 消息通道 | ✗ | ✗ | 飞书（MVP）/ 微信（远期） |
| 安全模型 | 无 | 无 | 沙箱 + 审批 + 租户隔离 |

---

## 2. 整体架构

5 层架构 + 可观测横切面：

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                    交互与通道层 (Interaction & Channel)                        │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────────────┐                │
│  │ CLI (TUI)│ │ 飞书 Bot │ │ Python   │ │  REST + WS API   │                │
│  │(Terminal)│ │ (Lark)   │ │  SDK     │ │  (FastAPI)       │                │
│  └────┬─────┘ └────┬─────┘ └────┬─────┘ └────────┬─────────┘                │
│       │            │            │                 │                           │
│       └────────────┼────────────┼─────────────────┘                           │
│                    │            │                                              │
│         ┌──────────┴────────────┴──────────┐                                  │
│         │       Channel Adapter            │  ← 统一消息路由/鉴权/限流         │
│         └──────────────┬───────────────────┘                                  │
├────────────────────────┼──────────────────────────────────────────────────────┤
│               编排与核心层 (Orchestration & Core)                               │
│  ┌─────────────────────┼──────────────────────────────────────────────┐       │
│  │               Swarm Orchestrator                                    │       │
│  │  handle_external_message() / run() / pause() / resume() / stop()   │       │
│  ├──────────┬──────────┬──────────┬──────────┬──────────┬─────────────┤       │
│  │ Delegate │Adversarial│  Skill   │  Sandbox │ Approval │  Security   │       │
│  │  Mode    │  Verify   │ Library  │  Manager │  Flow    │  Policy     │       │
│  └──────────┴──────────┴──────────┴──────────┴──────────┴─────────────┘       │
│  ┌──────────┬──────────┬──────────┬──────────┬──────────────────────┐        │
│  │   Task   │ Mailbox  │Knowledge │Conversat │ Session Manager      │        │
│  │  Queue   │  (P2P)   │  Base    │Context   │ (持久化 + 恢复)       │        │
│  └──────────┴──────────┴──────────┴──────────┴──────────────────────┘        │
├───────────────────────────────────────────────────────────────────────────────┤
│                        Agent Runtime (高性能)                                   │
│  ┌──────────────────────────────────────────────────────────────────────┐    │
│  │  Agent Loop (async, 连接池, 流式, token 预算)                          │    │
│  │  observe → think → act → reflect                                     │    │
│  └──────────────────────────────────────────────────────────────────────┘    │
├───────────────────────────────────────────────────────────────────────────────┤
│                   LLM Provider Layer (统一适配 + 连接池)                        │
│  ┌──────┬──────────┬──────────┬────────┬──────────┐                          │
│  │OpenAI│Anthropic │ DeepSeek │ Ollama │ Groq ...  │                          │
│  └──────┴──────────┴──────────┴────────┴──────────┘                          │
├───────────────────────────────────────────────────────────────────────────────┤
│                Storage (可插拔 + 命名空间隔离)                                   │
│  ┌──────────────┬──────────────┬──────────────┬──────────────┐                │
│  │ SQLite(file) │ SQLite(:mem:)│    Redis     │     File     │                │
│  │  默认/WAL    │  单元测试     │   生产分布式  │  零依赖降级   │                │
│  │  <5 agent    │  单进程       │   10+ agent  │  仅本地文件   │                │
│  └──────────────┴──────────────┴──────────────┴──────────────┘                │
└───────────────────────────────────────────────────────────────────────────────┘

可观测性（横切面）: ObservabilityBus → Metrics / Structured Logging / SQLite Session Store
```

---

## 3. 交互层设计

### 3.1 Python SDK

```python
from agent_swarm import Swarm

# 方式 1: 纯代码创建
swarm = Swarm(name="review-team")
swarm.add_agent("security-expert", persona="...", skills=["code-review:security"])

# 方式 2: YAML 声明式
swarm = Swarm.from_yaml("swarm.yaml")

# 方式 3: 从 session 恢复
swarm = Swarm.from_session("session_abc123")

# 运行时管理
await swarm.run()
await swarm.pause()
await swarm.resume()
await swarm.stop()
swarm.add_task(Task(title="新任务", ...))
status = swarm.status()  # → SwarmStatus
```

### 3.2 Swarm 编排器完整 API

```python
class Swarm:
    """Swarm 编排器——交互层和核心层的桥梁"""

    # === 生命周期 ===
    async def run(self) -> SwarmResult: ...
    async def start(self) -> None: ...
    async def pause(self) -> None: ...
    async def resume(self) -> None: ...
    async def stop(self, force: bool = False) -> None: ...

    # === 运行时管理 ===
    def add_agent(self, agent: Agent) -> None: ...
    def get_agent(self, agent_id: str) -> Agent | None: ...
    def remove_agent(self, agent_id: str) -> None: ...
    def add_task(self, task: Task) -> None: ...
    def remove_task(self, task_id: str) -> None: ...
    def status(self) -> SwarmStatus: ...

    # === 外部消息注入（消息通道层通过此接口与 swarm 交互）===
    async def handle_external_message(self, msg: ChannelMessage) -> ChannelResponse:
        """
        处理来自消息通道的外部消息。
        这是消息通道层进入编排层的唯一入口。

        流程：
        1. 解析用户意图（@agent、命令、普通对话）
        2. 查找/创建 swarm session
        3. 将消息注入对应 agent 的 mailbox（创建 Message，target_type=EXTERNAL）
        4. 等待 agent 处理并生成响应
        5. 将响应转换为 ChannelResponse 返回
        """

    # === 协议选择 ===
    def set_protocol(self, protocol: CollaborationProtocol) -> None:
        """设置协作协议（DelegateMode / AdversarialVerifier）"""
```

### 3.3 CLI (Terminal TUI)

```bash
# 快速启动
agent-swarm start --config swarm.yaml

# 交互式创建
agent-swarm create

# 管理命令
agent-swarm list                # 列出所有 swarm
agent-swarm attach <swarm-id>   # 接入运行中的 swarm
agent-swarm session resume <id> # 恢复会话
agent-swarm channel test lark   # 测试飞书通道连通性

# 可观测
agent-swarm monitor             # 实时 TUI 仪表盘
agent-swarm logs <agent-id>     # 查看 agent 日志
```

TUI 界面示意（基于 Textual）：

```
┌─ Agent Swarm Monitor ─────────────────────────────────────────┐
│ Swarm: review-team  │ Session: s_abc123  │ Uptime: 12m 34s    │
├────────────────────────────────────────────────────────────────┤
│ ┌─ Task Queue ──────────┐  ┌─ Agent Status ─────────────────┐ │
│ │ ✓ PR安全审查  sec-1    │  │ sec-1  ● active  │ 12.3k tok │ │
│ │ ◐ PR性能审查 perf-1    │  │ perf-1 ● active  │  8.7k tok │ │
│ │ ○ PR测试审查 test-1    │  │ test-1 ○ idle    │  0.0k tok │ │
│ │ ✓ 汇总报告   lead-1   │  │ lead-1 ✓ done    │  2.1k tok │ │
│ └────────────────────────┘  └───────────────────────────────┘ │
│ ┌─ Mailbox (Last Messages) ──────────────────────────────────┐ │
│ │ sec-1 → perf-1 [challenge] "你审了 auth.py:42 吗？疑似注入" │ │
│ │ perf-1 → sec-1 [reply]     "已确认，是 false positive"      │ │
│ └────────────────────────────────────────────────────────────┘ │
│ [Tab]切换视图 [q]退出 [s]发消息 [t]任务详情                      │
└────────────────────────────────────────────────────────────────┘
```

---

## 4. 消息通道层设计

### 4.1 设计理念

消息通道层让 agent-swarm 从"开发者工具"变为"可嵌入协作平台的 AI 团队成员"。用户通过飞书等日常工具直接与 swarm 交互。

```
普通用户（非开发者）                  开发者
      │                              │
      ▼                              ▼
┌──────────┐                   ┌──────────────┐
│ 飞书群   │                   │ CLI / SDK    │
│ @机器人  │                   │              │
└────┬─────┘                   └──────┬───────┘
     │                               │
     ▼                               ▼
┌─────────────────────────────────────────┐
│           Channel Adapter               │
│  统一消息路由 · 鉴权 · 限流 · 会话绑定    │
└────────────────────┬────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────┐
│     Swarm.handle_external_message()     │
│     唯一入口，连接通道层与编排层          │
└─────────────────────────────────────────┘
```

### 4.2 统一消息模型与通道抽象

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Awaitable

class ChannelType(Enum):
    LARK = "lark"
    REST_API = "rest_api"
    WEB_SOCKET = "web_socket"
    CLI = "cli"
    SDK = "sdk"

class MessageType(Enum):
    TEXT = "text"
    IMAGE = "image"
    FILE = "file"
    CARD = "card"
    EVENT = "event"
    COMMAND = "command"

@dataclass
class ChannelUser:
    """通道用户标识"""
    channel: ChannelType
    user_id: str
    display_name: str
    extra: dict[str, Any] = field(default_factory=dict)

@dataclass
class ChannelMessage:
    """统一消息格式——所有通道的消息归一化为此结构"""
    id: str
    channel: ChannelType
    from_user: ChannelUser
    content: str
    msg_type: MessageType = MessageType.TEXT
    media_urls: list[str] = field(default_factory=list)
    reply_to: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    timestamp: float = 0.0

@dataclass
class ChannelResponse:
    """统一响应格式——swarm 回复归一化后由各通道适配器渲染"""
    content: str
    msg_type: MessageType = MessageType.TEXT
    card_template: str | None = None
    card_data: dict[str, Any] | None = None
    media_urls: list[str] = field(default_factory=list)
    mentions: list[str] = field(default_factory=list)
    reply_to: str | None = None

# ====== 通道连接器抽象 ======
class ChannelConnector(ABC):
    """
    消息通道连接器基类

    每个通道实现自己的连接器，负责：
    1. 接收消息 → 归一化为 ChannelMessage
    2. 发送响应 → 将 ChannelResponse 渲染为通道原生格式
    3. 会话管理 → 绑定通道用户与 swarm session
    """

    @property
    @abstractmethod
    def channel_type(self) -> ChannelType: ...

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def send(self, response: ChannelResponse,
                   target: ChannelUser | str) -> bool: ...

    @abstractmethod
    def subscribe(self, handler: "MessageHandler") -> None: ...

    @abstractmethod
    def unsubscribe(self, handler: "MessageHandler") -> None: ...
```

### 4.3 ChannelAdapter（统一路由 + 鉴权）

```python
class ChannelAdapter:
    """
    统一消息通道适配器

    职责:
    - 管理多个 ChannelConnector 实例
    - 消息路由：ChannelMessage → Swarm.handle_external_message() → ChannelResponse
    - 会话绑定：通道用户 ↔ swarm session
    - 鉴权与限流
    """

    def __init__(self):
        self._connectors: dict[ChannelType, ChannelConnector] = {}
        self._session_bindings: SessionBindingManager
        self._rate_limiters: dict[str, RateLimiter] = {}
        self._user_whitelist: set[str] = set()       # IM 用户白名单
        self._api_key_store: APIKeyStore             # REST API 密钥管理

    def register_connector(self, connector: ChannelConnector) -> None: ...

    async def route_message(self, msg: ChannelMessage) -> ChannelResponse:
        """
        核心路由逻辑：
        1. 鉴权检查（IM 用户白名单 / API Key）
        2. 根据 from_user 查找绑定的 swarm session
        3. 如无绑定 → 创建新 session
        4. 调用 swarm.handle_external_message(msg)
        5. 返回 ChannelResponse
        """

    async def bind_user_session(self, user: ChannelUser,
                                 session_id: str) -> None: ...
```

### 4.4 飞书 (Lark) 连接器（MVP 唯一消息通道）

```python
class LarkConnector(ChannelConnector):
    """
    飞书消息通道连接器

    支持场景:
    - 飞书群机器人：群内 @机器人 触发对话
    - 飞书应用：用户直接与飞书应用对话
    - 飞书卡片：交互式卡片消息（按钮、表单）

    安全措施:
    - 事件回调强制验证签名（verification_token）
    - 支持用户白名单：只有白名单用户可以与 swarm 交互
    """

    channel_type = ChannelType.LARK

    def __init__(
        self,
        app_id: str,
        app_secret: str,                       # 从 SecretManager 获取
        verification_token: str,               # 事件签名验证（必填）
        encrypt_key: str | None = None,
        user_whitelist: list[str] | None = None,  # IM 用户白名单
    ): ...

    async def send_card(self, card_template: str, card_data: dict,
                        target: str) -> bool:
        """
        内置卡片模板：
        - task_progress: 任务进度看板
        - code_review_result: 代码审查结果
        - adversarial_debug: 对抗式调试进度
        - swarm_status: Swarm 运行状态
        - confirm_dialog: 确认对话框（Human-in-the-loop）
        """

    async def handle_card_action(self, action: dict) -> ChannelMessage:
        """处理卡片交互 → 转为 ChannelMessage，强制验证签名"""
```

> **远期扩展**: 微信（公众号/企业微信）连接器、Slack/Discord 连接器在飞书通道验证可行后按需添加。

### 4.5 消息路由：通道消息 → Agent 对话

```python
class MessageRouter:
    """
    核心映射逻辑：
    ┌─────────────────┬──────────────────────────────┐
    │ 通道消息行为      │ Swarm 内部动作                │
    ├─────────────────┼──────────────────────────────┤
    │ 用户发文本消息    │ → Message(target_type=EXTERNAL, to_agent=lead) │
    │ @特定 agent      │ → Message(target_type=EXTERNAL, to_agent=agent) │
    │ 点击"确认"按钮   │ → 触发 ApprovalFlow 回调       │
    │ 点击"拒绝"按钮   │ → 触发 ApprovalFlow 回调       │
    │ 发图片/文件      │ → 作为 agent 工具调用的输入     │
    │ 用户发"状态"     │ → 查询 swarm.status() 并回复    │
    └─────────────────┴──────────────────────────────┘
    """
```

### 4.6 消息通道配置

```yaml
channels:
  lark:
    enabled: true
    app_id: "cli_xxxxxxxx"
    app_secret: "${LARK_APP_SECRET}"
    verification_token: "${LARK_VERIFICATION_TOKEN}"
    user_whitelist:                   # IM 用户白名单
      - "ou_abc123"
      - "ou_def456"

  # 通用通道配置
  routing:
    default_session_ttl: 3600
    max_sessions_per_user: 5
    rate_limit:
      messages_per_minute: 30
      sessions_per_hour: 10
    approval_timeout: 3600

  notifications:
    on_task_complete: true
    on_approval_required: true
    on_error: true
    progress_interval: 300
```

---

## 5. 可观测性（横切面）

> 可观测性是横切关注点，以 ObservabilityBus 形式注入各层，不占独立架构层。

### 5.1 指标维度

```python
@dataclass
class FrameworkMetrics:
    uptime_seconds: float
    active_swarms: int
    active_agents: int
    tasks_total: int
    tasks_completed: int
    messages_total: int
    llm_calls_total: int
    llm_tokens_total: int
    llm_cost_estimate: float
    errors_total: int
    avg_task_latency_ms: float

@dataclass
class AgentMetrics:
    agent_id: str
    agent_role: str
    status: str                        # idle | thinking | acting | waiting
    current_task_id: str | None
    session_id: str
    llm_calls: int
    tokens_prompt: int
    tokens_completion: int
    tokens_total: int
    cost_estimate: float
    tool_calls: int
    tool_errors: int
    messages_sent: int
    messages_received: int
    avg_loop_latency_ms: float
    context_window_usage_pct: float

@dataclass
class SessionMetrics:
    session_id: str
    swarm_name: str
    status: str
    agents: list[str]
    tasks: list[str]
    started_at: float
    ended_at: float | None
    duration_seconds: float
    events: list[SessionEvent]         # 用于回放
```

### 5.2 可观测总线

```python
class ObservabilityBus:
    """
    统一可观测总线（横切面）

    设计原则:
    - 单例：通过 contextvars 注入，所有模块通过 obs_bus.current() 获取
    - 同步 emit + 异步 dispatch：emit 永不阻塞业务路径
    - 订阅者解耦：Sink 只关心自己感兴趣的事件类型
    """
    def emit_metric(self, name: str, value: float, labels: dict): ...
    def emit_event(self, event: SessionEvent): ...
    def emit_log(self, level: str, message: str, extra: dict): ...

    def register_sink(self, sink: "ObservabilitySink",
                       event_filter: set[str] | None = None) -> None:
        """订阅者注册（event_filter 接受事件名 glob，如 {"task.*", "verifier.*"}；None=全订阅）"""

class ObservabilitySink(ABC):
    """事件接收方抽象"""
    async def consume(self, event: SessionEvent) -> None: ...
```

### 5.3 默认订阅者（启动时自动注册）

| Sink | 订阅事件 | 用途 | 默认状态 |
|------|---------|------|---------|
| `JsonLogSink` | 全部 | 结构化 JSON 日志输出到 stdout/file | 开启 |
| `SqliteEventSink` | 全部 | 写入 session_events 表（用于恢复 + 回放） | 开启 |
| `MetricsSink` | metric_* | 内存计数器，供 `swarm.status()` 查询 | 开启 |
| `WebSocketSink` | 全部 | 推送给 TUI / GUI | TUI 启动时注册 |
| `PrometheusSink` | metric_* | 暴露 /metrics 端点 | 配置开启 |

### 5.4 事件命名目录与 emit 点（落地保证）

> 此小节是 v4 新增——确保 ObservabilityBus 不是"被设计但不被使用"的抽象。

**事件命名规范**：`{layer}.{module}.{action}`

| 事件名 | emit 调用方（具体代码位置） | 时机 |
|--------|----------|------|
| `agent.loop.observe_start` | `Agent._observe()` 入口 | 每次循环开始 |
| `agent.loop.think_start` | `Agent._think()` 入口 | LLM 调用前 |
| `agent.loop.act_start` | `Agent._act()` 入口 | 工具调用前 |
| `agent.loop.iteration_complete` | `Agent.loop()` 每轮末尾 | 一轮完整 OTAR 结束 |
| `agent.error.tool_failed` | `Agent._act()` 异常分支 | 工具执行失败 |
| `task.created` | `TaskQueue.add()` | 任务入队 |
| `task.claimed` | `TaskQueue.claim()` 成功后 | 任务被认领 |
| `task.completed` | `TaskQueue.complete()` 成功后 | 任务完成 |
| `task.cas_conflict` | `TaskQueue.complete()` 版本冲突 | 乐观锁失败（重要！排查并发问题） |
| `message.sent` | `Mailbox.send()` 末尾 | 消息发出 |
| `message.received` | `Mailbox.receive()` 返回非空 | 消息被消费 |
| `llm.call_start` | `LLMProvider.chat()` 入口 | LLM 调用 |
| `llm.call_complete` | `LLMProvider.chat()` 返回 | LLM 调用完成（含 token 数） |
| `llm.call_failed` | `LLMProvider.chat()` 异常 | LLM 失败（用于 circuit breaker） |
| `security.policy_check` | `SecurityPolicy.check_tool()` 返回 | 每次工具调用前 |
| `security.approval_requested` | `ApprovalFlow.request_approval()` | 审批发起 |
| `security.approval_granted` | 审批回调成功分支 | 审批通过 |
| `security.approval_denied` | 审批回调拒绝/超时分支 | 审批拒绝 |
| `verifier.round_start` | `AdversarialVerifier._run_round()` | 对抗式验证一轮开始 |
| `verifier.hypothesis_eliminated` | 假设被淘汰时 | 用于回放谁淘汰了谁 |
| `verifier.verdict_reached` | `verify()` 返回前 | 最终结论 |
| `swarm.started` / `swarm.stopped` / `swarm.paused` | `Swarm` 生命周期方法 | swarm 状态变更 |
| `channel.message_in` | `ChannelAdapter.route_message()` 入口 | 外部消息进入 |
| `channel.message_out` | `ChannelConnector.send()` 末尾 | 响应送出 |

**示例：Agent loop 中的 emit 调用**

```python
class Agent:
    async def loop(self):
        bus = ObservabilityBus.current()
        while self.active:
            t0 = monotonic()
            bus.emit_event(SessionEvent(
                event_name="agent.loop.observe_start",
                session_id=self.session_id,
                timestamp=time(),
                payload={"agent_id": self.id},
            ))
            obs = await self._observe()

            bus.emit_event(SessionEvent(
                event_name="agent.loop.think_start",
                session_id=self.session_id,
                timestamp=time(),
                payload={"agent_id": self.id},
            ))
            action = await self._think(obs)

            bus.emit_event(SessionEvent(
                event_name="agent.loop.act_start",
                session_id=self.session_id,
                timestamp=time(),
                payload={"agent_id": self.id, "action_type": action.type},
            ))
            try:
                result = await self._act(action)
            except ToolError as e:
                bus.emit_event(SessionEvent(
                    event_name="agent.error.tool_failed",
                    session_id=self.session_id,
                    timestamp=time(),
                    payload={"agent_id": self.id, "error": str(e)},
                ))
                raise

            await self._reflect(result)
            bus.emit_metric("agent.loop.latency_ms",
                            (monotonic() - t0) * 1000, {"agent_id": self.id})
            bus.emit_event(SessionEvent(
                event_name="agent.loop.iteration_complete",
                session_id=self.session_id,
                timestamp=time(),
                payload={"agent_id": self.id},
            ))
```

**Session 恢复的事件来源**：`SqliteEventSink` 持久化的事件流即 `SessionManager.restore_session()` 读取的源——一套事件流同时支撑日志、回放、恢复，无重复机制。

---

## 6. 编排与核心模块

> 编排层和核心模块合并为同一层。Swarm Orchestrator 是总入口，Task Queue / Mailbox / KnowledgeBase / ConversationContext 是其管理的核心组件。

### 6.1 委托模式 (Delegate Mode)

> v4 修订：不再引入 LeadAgent / WorkerAgent 子类——通过 §7.1 的 `AgentCapabilities.lead()` / `.worker()` 预设来表达，Agent 类保持单一。
> v4.1 修订：协议基类从 `Protocol` 改名为 `CollaborationProtocol`，避免与 stdlib `typing.Protocol` 撞名。

```python
class DelegateMode(CollaborationProtocol):
    """
    委托协议：要求 swarm 中存在至少一个 lead capabilities 的 agent，
    其余为 worker capabilities。lead 不执行任务，只负责派发与汇总。

    调用方通过 AgentCapabilities.lead() / .worker(tools=...) 创建对应 agent。
    """
    async def execute(self, swarm: "Swarm") -> ProtocolResult: ...
```

### 6.2 对抗式验证 (Adversarial Verify)

> v4 修订：补完一轮的完整流程、判定算法、终止条件与失败兜底。
> v4.1 修订：基类改名 `CollaborationProtocol`；澄清 Judgement 不进入 ConversationContext.history（见 §6.6）。

#### 6.2.1 核心思想

每个 agent **对每个假设**独立给出"支持/反驳/不确定 + 置信度 + 证据"，多轮迭代后：被多数 agent 反驳的假设淘汰，最终留下被一致支持的假设。**关键创新**：每轮都让 agent 看到他人观点（含证据），允许立场更新——这与单轮多人投票本质不同。

#### 6.2.2 数据结构

```python
class Stance(Enum):
    SUPPORT = "support"          # 支持（找到了证据）
    REFUTE = "refute"            # 反驳（找到了反例）
    UNCERTAIN = "uncertain"      # 不确定（证据不足）

@dataclass
class Judgement:
    """单个 agent 对单个假设在某一轮的判断"""
    agent_id: str
    hypothesis_id: str
    round_no: int
    stance: Stance
    confidence: float            # 0.0 ~ 1.0
    evidence: list[str]          # 证据列表（文件路径/日志片段/工具输出引用）
    reasoning: str               # 简要推理

@dataclass
class HypothesisState:
    """假设在某一轮结束后的状态"""
    id: str
    statement: str
    eliminated: bool = False
    eliminated_at_round: int | None = None
    judgements_by_round: dict[int, list[Judgement]] = field(default_factory=dict)

    def support_score(self, round_no: int) -> float:
        """加权支持度：support 加分，refute 扣分，按 confidence 加权"""
        js = self.judgements_by_round.get(round_no, [])
        if not js:
            return 0.0
        score = sum(
            j.confidence * (1 if j.stance == Stance.SUPPORT
                            else -1 if j.stance == Stance.REFUTE else 0)
            for j in js
        )
        return score / len(js)        # 归一化到 [-1, 1]

@dataclass
class Verdict:
    survivors: list[HypothesisState]      # 存活假设（按支持度排序）
    eliminated: list[HypothesisState]
    rounds_used: int
    convergence_reason: Literal[
        "min_survivors_reached",          # 假设数 ≤ min_survivors
        "consensus_stable",               # 连续 2 轮无淘汰
        "max_rounds_exhausted",           # 达到 max_rounds
        "all_eliminated",                 # 全部被淘汰（兜底）
    ]
    root_cause: str | None                # 当 survivors == 1 时的最终结论
    confidence: float                     # 最终置信度
    full_history: list[Judgement]         # 完整事件流（emit 到 ObservabilityBus）
```

#### 6.2.3 一轮的完整流程

```
Round N:
  1. 广播阶段：将所有【未淘汰】假设 + 上一轮所有 agent 的 Judgement（脱敏后）
     注入每个 agent 的 ConversationContext.external_inputs（独立字段，不污染 history）。
     ↑ 关键：history 仍然是 per-agent 隔离的"自由对话"，Judgement 是结构化外部观察
       两者分开存放，由 LLM prompt 模板分别渲染（见 §6.6）
     —— Prompt 模板: "以下是上一轮其他专家的判断，请独立审视并更新你的看法"

  2. 判断阶段（并行）：每个 agent 对每个未淘汰假设产出 Judgement。
     —— LLM 调用强制使用 structured output（JSON Schema 约束 stance/confidence/evidence）。

  3. 评分阶段：对每个假设计算 support_score(N)。

  4. 淘汰阶段：满足以下任一条件的假设被淘汰
     a) support_score(N) <= ELIMINATE_THRESHOLD（默认 -0.5）
     b) 连续 2 轮 support_score 低于 0（一直被反驳）
     c) 没有任何 agent 给出 SUPPORT 立场

  5. 收敛检查：见 §6.2.4
```

#### 6.2.4 终止条件（按优先级）

| 优先级 | 条件 | 说明 |
|------|------|------|
| 1 | `len(survivors) <= min_survivors` | 已达到目标存活数 |
| 2 | 连续 2 轮无任何假设被淘汰 + 无任何 agent 改变立场 | 共识稳定 |
| 3 | `round_no >= max_rounds` | 强制截断 |
| 4 | `len(survivors) == 0` | 全淘汰兜底 → 见 §6.2.5 |

#### 6.2.5 失败兜底

| 场景 | 处理 |
|------|------|
| 全部假设被淘汰 | 返回 `convergence_reason="all_eliminated"`，`root_cause=None`，并将"被淘汰最晚"的假设作为弱推荐返回 |
| 达到 max_rounds 仍有 ≥2 假设存活 | 按 `support_score` 降序排列，全部返回；不强行选 1 |
| 某个 agent 在某轮返回非法 JSON | 重试 1 次，仍失败则该 agent 在该轮该假设的 stance 计为 `UNCERTAIN`（不影响其他 agent） |
| 所有 agent 在某轮全部失败 | 该轮作废，`max_rounds` 不计入；连续 2 轮全部失败 → 抛 `VerifierStallError` |

#### 6.2.6 API

```python
class AdversarialVerifier(CollaborationProtocol):
    def __init__(
        self,
        min_survivors: int = 1,
        max_rounds: int = 5,
        eliminate_threshold: float = -0.5,
        per_round_timeout: float = 120.0,
    ): ...

    async def verify(
        self,
        hypotheses: list[str],
        agents: list[Agent],
    ) -> Verdict: ...
```

**复杂度**：`O(rounds × agents × hypotheses)` 次 LLM 调用。建议 `agents × hypotheses ≤ 25` 以控成本（5×5 是甜蜜点）。

### 6.3 协议注册机制

```python
class CollaborationProtocol(ABC):
    """
    协作协议基类（v4.1：从 Protocol 改名，避免与 typing.Protocol 撞名）

    所有具体协议（DelegateMode / AdversarialVerifier / ...）继承此类。
    """
    @abstractmethod
    async def execute(self, swarm: "Swarm") -> ProtocolResult: ...

# Swarm 中注册协议
swarm.set_protocol(DelegateMode())
swarm.set_protocol(AdversarialVerifier(
    max_rounds=5, min_survivors=1
))
```

### 6.4 Task Queue（统一乐观锁 CAS）

> v4 修订：v3 同时使用悲观锁（`acquire_lock`）+ 乐观锁（`version`），并发模型不清。
> v4 统一为**乐观锁 CAS 单一并发模型**，`acquire_lock` 降级为通用基建（仅供需要分布式互斥的特殊场景使用，**不参与任务认领**）。

#### 6.4.1 数据结构

```python
@dataclass
class Task:
    id: str
    title: str
    description: str
    status: Literal["pending", "blocked", "in_progress", "completed", "failed"]
    depends_on: list[str]
    assigned_to: str | None
    assigned_skill: str | None
    result: Any | None
    version: int = 0                  # ← 唯一的并发控制字段
    created_at: float = 0.0
    updated_at: float = 0.0
    # 注：tenant_id 不再出现在领域对象中——通过 SecurityContext 隐式传递（见 §8.4）
```

#### 6.4.2 单一并发模型：CAS（Compare-And-Swap）

所有状态变更走同一条路径——**带版本号的条件更新**：

```sql
UPDATE tasks
   SET status = ?, assigned_to = ?, version = version + 1, updated_at = ?
 WHERE id = ?
   AND version = ?           -- ← CAS：版本号必须匹配
   AND tenant_id = ?         -- ← 租户隔离（来自 SecurityContext）
```

返回受影响行数：1 = 成功，0 = 冲突（其他 agent 抢先）。

#### 6.4.3 API

```python
@dataclass
class ClaimResult:
    """
    任务认领结果——明确区分三种失败原因（v4.1 引入）

    v4 用 Optional[Task] 表示成功/失败，但 None 同时混淆了
    "任务不存在" / "已被认领" / "版本号不匹配" 三种情况，排错困难。
    """
    success: bool
    task: Task | None                # 成功时为更新后的 Task
    reason: Literal[
        "ok",
        "task_not_found",            # task_id 不存在
        "version_mismatch",          # 版本号已被其他 agent 推进（CAS 冲突）
        "already_claimed",           # 任务已是 in_progress
        "dependency_blocked",        # 依赖任务未完成
    ] = "ok"

class TaskQueue:
    """声明式状态账本，单一乐观锁并发模型"""

    async def add(self, task: Task) -> str: ...

    async def claim(self, task_id: str, agent_id: str,
                     expected_version: int) -> ClaimResult:
        """
        认领任务：CAS 更新 status=pending → in_progress, assigned_to=agent_id
        - 失败时通过 ClaimResult.reason 区分原因
        - 同时 emit `task.cas_conflict` 事件（version_mismatch 时）供可观测排查
        - agent 应重新拉取任务列表后再尝试，不重试
        """

    async def complete(self, task_id: str, result: Any,
                        expected_version: int) -> ClaimResult:
        """CAS 更新 status=in_progress → completed；冲突时返回 reason="version_mismatch" """

    async def fail(self, task_id: str, error: str,
                    expected_version: int) -> ClaimResult: ...

    async def list_claimable(self) -> list[Task]:
        """
        列出当前可认领的任务（status=pending 且依赖全部 completed）
        每个 Task 带当前 version，agent 用此 version 调 claim()
        """
```

#### 6.4.4 认领流程（Agent 侧）

```python
# Agent 主循环中的标准模式
async def _try_claim_task(self) -> Task | None:
    candidates = await self.task_queue.list_claimable()
    for task in self._rank_by_skill(candidates):       # 按技能匹配排序
        result = await self.task_queue.claim(
            task.id, self.id, expected_version=task.version
        )
        if result.success:
            return result.task
        # 冲突：通过 result.reason 排查（version_mismatch / already_claimed / ...）
        # 同时 ObservabilityBus 已 emit task.cas_conflict 事件，可观测层会记录
        continue
    return None                                        # 没有可认领的
```

**为什么不需要悲观锁？**
- 任务认领是"可重试"的——抢不到就换一个，不会"因为没拿到锁而阻塞"
- 乐观锁在 SQLite/Redis 上原生支持，无需额外的锁表/锁 key 管理
- 减少了一类故障：锁泄漏（agent 崩溃后锁卡死）

> v4.1 修订：v4 保留的 `StorageBackend.acquire_lock` 已从 MVP ABC 中删除（YAGNI）。如果远期出现明确的分布式互斥需求（leader 选举、跨 swarm 文件互斥），届时再作为可选扩展加回。

### 6.5 Mailbox（点对点消息）

```python
class TargetType(Enum):
    INTERNAL = "internal"    # agent 间通信
    EXTERNAL = "external"    # 回复外部用户（消息通道）

@dataclass
class Message:
    id: str
    from_agent: str
    to_agent: str | None              # None = broadcast
    target_type: TargetType
    msg_type: Literal["question", "challenge", "reply", "notify", "delegate"]
    content: str
    refs: list[str]
    timestamp: float
    reply_to: str | None
    read: bool = False
    # 注：tenant_id 通过 SecurityContext 隐式传递（见 §8.4），不在领域对象中

class Mailbox:
    """agent 间直通消息系统"""

    async def send(self, msg: Message) -> None: ...
    async def receive(self, agent_id: str, since: float = None,
                       msg_type: str = None) -> list[Message]: ...
    async def receive_by_ref(self, agent_id: str, ref_id: str) -> list[Message]: ...
    async def get_thread(self, msg_id: str) -> list[Message]: ...
    async def mark_read(self, agent_id: str, message_ids: list[str]) -> None: ...
    async def reply_to(self, msg_id: str, content: str) -> None: ...
    async def broadcast(self, from_agent: str, content: str) -> None: ...
```

### 6.6 KnowledgeBase（共享知识） + ConversationContext（隔离上下文）

> v4 修订：补充拆分动机的具体场景。
> v4.1 修订：
> - ConversationContext 增加 `external_inputs` 字段，用于承载 AdversarialVerifier 的 Judgement 等"结构化外部观察"，**不污染对话历史**。
> - KnowledgeBase 明确为 **per-tenant 实例**，避免跨租户信息泄露。

#### 6.6.1 拆分必要性（三条不可合并的硬约束）

| # | 场景 | 为什么必须分开 |
|---|------|------|
| 1 | **多 agent 缓存一致性** | KnowledgeBase 是写多读多（A 分析过 `auth.py` → B 复用结果）；ConversationContext 是单写者（每个 agent 自己追加）。合一后要么牺牲一致性、要么牺牲性能。分开后 KB 用 RWLock，CC 无锁。 |
| 2 | **不同的截断策略** | KB 按重要性裁剪（LRU + size cap）；CC 按时间窗口 + token 预算裁剪（保留最近 N 轮 + 摘要）。合一后无法用单一策略——要么把项目文档摘要掉（错），要么不截断 agent 历史（爆 token）。 |
| 3 | **对抗式验证的隔离要求** | AdversarialVerifier 要求每个 agent 看到的对话历史**只有自己的**——这是对抗式的核心（独立判断）。但每个 agent **必须**看到相同的项目知识，并且需要看到**结构化的同伴判断**（Judgement，不是自由对话）。合一后无法区分这三类数据。 |

合并方案（一个 Context 含 `shared` 和 `per_agent` 子命名空间）已被否决——上述三条约束让"统一访问层"变成壳，内部仍是两个独立子系统。

**三类数据的存放位置**（v4.1 澄清）：

| 数据类型 | 位置 | 隔离粒度 |
|---------|------|---------|
| 项目知识、文件分析缓存 | `KnowledgeBase` | per-tenant（同一 tenant 内所有 agent 共享） |
| Agent 自由对话历史（user/assistant turns） | `ConversationContext.history` | per-agent（绝对隔离） |
| 结构化外部观察（Judgement、审批结果、外部用户消息摘要） | `ConversationContext.external_inputs` | per-agent，但内容由编排层注入 |

#### 6.6.2 接口

```python
class KnowledgeBase:
    """
    共享知识层（per-tenant 实例，所有同租户 agent 可见，写多读多，RWLock 保护）

    v4.1 修订：明确为 per-tenant——每个 tenant 持有独立的 KnowledgeBase 实例，
    不存在跨租户读取路径，避免信息泄露。

    - 项目文档 / 约定
    - 文件系统索引
    - Task Queue 状态快照
    - 共享的代码分析结果缓存（A 分析过的，B 直接用）
    """

    async def get_project_docs(self) -> list[Document]: ...
    async def search_code(self, query: str) -> list[CodeSnippet]: ...
    async def get_task_status(self) -> list[Task]: ...
    async def cache_analysis(self, key: str, result: Any,
                              ttl: float | None = None) -> None: ...
    async def get_cached_analysis(self, key: str) -> Any | None: ...

    # 截断策略：LRU + size cap（默认 100MB）
    def configure_eviction(self, max_size_bytes: int,
                            policy: Literal["lru", "lfu"] = "lru"): ...

class KnowledgeBaseRegistry:
    """
    KB 实例工厂——按 tenant_id 分发独立实例（v4.1 引入）

    Swarm 启动时通过 SecurityContextManager.current().tenant_id 获取自己的 KB。
    """
    def get_for_current_tenant(self) -> KnowledgeBase: ...

@dataclass
class Turn:
    """对话历史的一轮——agent 自由对话"""
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    timestamp: float = 0.0

@dataclass
class ExternalInput:
    """
    结构化外部观察（v4.1 引入）——区别于自由对话

    用于承载：
    - AdversarialVerifier 注入的同伴 Judgement
    - ApprovalFlow 的批准/拒绝结果
    - 外部用户消息的结构化摘要
    """
    source: Literal["verifier", "approval", "external_user", "system"]
    payload: dict[str, Any]              # 结构化内容
    round_no: int | None = None          # 在 verifier 场景下标记轮次
    timestamp: float = 0.0

class ConversationContext:
    """
    隔离上下文（每个 agent 独享，单写者无锁）

    v4.1 修订：拆分为 history（自由对话）+ external_inputs（结构化观察）
    LLM prompt 模板分别渲染两者：history 直接拼入 messages，
    external_inputs 渲染为系统消息中的"外部观察"段落。
    """

    # === 自由对话历史 ===
    async def get_history(self, agent_id: str) -> list[Turn]: ...
    async def append(self, agent_id: str, turn: Turn) -> None: ...
    async def summarize(self, agent_id: str) -> str: ...
    async def get_token_usage(self, agent_id: str) -> int: ...

    # === 结构化外部观察（v4.1 新增，不进入 history） ===
    async def push_external_input(self, agent_id: str,
                                    item: ExternalInput) -> None: ...
    async def get_external_inputs(self, agent_id: str,
                                    source: str | None = None) -> list[ExternalInput]: ...
    async def clear_external_inputs(self, agent_id: str,
                                     source: str | None = None) -> None: ...

    # 截断策略：token 预算 + 时间窗口（与 TokenBudgetManager 协作）
    async def truncate_for_budget(self, agent_id: str,
                                    target_tokens: int) -> None: ...
```

### 6.7 Session Manager（会话持久化）

> v4.1 修订：删除 `SessionEventType` enum——事件名采用 §5.4 的字符串规范（`{layer}.{module}.{action}`），新增类型时无需改 enum，天然可扩展。`SessionEvent` 改为携带 `event_name: str`。

```python
@dataclass
class SessionEvent:
    """统一事件结构（与 §5.4 命名规范一致）"""
    event_name: str               # 例如 "task.created" / "agent.loop.iteration_complete"
    session_id: str
    timestamp: float
    payload: dict[str, Any]       # 事件载荷（agent_id / task_id / error / ...）
    request_id: str | None = None # 关联 SecurityContext.request_id

class SessionManager:
    """
    会话持久化与恢复

    关键设计:
    - 所有状态变更以事件流形式持久化（任务级粒度）
    - 恢复时重放事件流重建状态
    - 支持跨进程恢复（SQLite WAL 模式）
    - 事件流写入由 ObservabilityBus.SqliteEventSink 完成（统一通道，不重复实现）
    """

    async def create_session(self, swarm_config: dict) -> str: ...
    async def restore_session(self, session_id: str) -> dict:
        """从 SqliteEventSink 持久化的事件流重放，重建 swarm 状态"""
    async def list_sessions(self) -> list[SessionSummary]:
        """tenant_id 由 SecurityContextManager 隐式提供（见 §8.4）"""
```

> 注：`save_event` / `save_events_batch` 不再由 SessionManager 直接暴露——事件统一通过 `ObservabilityBus.emit_event()` 流入 `SqliteEventSink`，SessionManager 只负责创建/恢复/列举。

---

## 7. Agent Runtime

### 7.1 Agent 生命周期 + 能力模型（Capabilities）

> v4 修订：v3 的 `mode` / `allowed_actions` / `risk_profile` 三个机制语义重叠。
> v4 合并为单一 `AgentCapabilities`——能力是行为的唯一权威来源；预设 profile 是 capabilities 的派生。

```python
@dataclass
class AgentCapabilities:
    """
    Agent 能力清单——单一权威来源

    替代 v3 的 mode + allowed_actions + risk_profile。
    所有"agent 能做什么"的判断都查这一个对象。
    """
    # 工具白名单（按工具 ID）
    allowed_tools: set[str]

    # 编排能力（取代 v3 的 mode 字段）
    can_spawn_agents: bool = False           # 等价于旧 mode=delegate 的能力
    can_shutdown_agents: bool = False
    can_assign_tasks: bool = False
    can_execute_actions: bool = True         # False 等价于旧 mode=plan_only

    # 风险等级上限（取代 v3 的 risk_profile）
    max_tool_risk: ToolRisk = ToolRisk.MEDIUM   # 高于此等级的工具自动拒绝

    # 资源配额
    max_tokens_per_task: int = 100_000
    max_concurrent_tool_calls: int = 3

    @classmethod
    def lead(cls) -> "AgentCapabilities":
        """预设：协调者（只编排不执行）"""
        return cls(
            allowed_tools={"send_message", "review_plan", "update_task"},
            can_spawn_agents=True,
            can_shutdown_agents=True,
            can_assign_tasks=True,
            can_execute_actions=False,
            max_tool_risk=ToolRisk.LOW,
        )

    @classmethod
    def worker(cls, tools: set[str],
                 max_risk: ToolRisk = ToolRisk.MEDIUM) -> "AgentCapabilities":
        """预设：执行者（只执行不编排）"""
        return cls(
            allowed_tools=tools,
            can_spawn_agents=False,
            can_assign_tasks=False,
            can_execute_actions=True,
            max_tool_risk=max_risk,
        )

    @classmethod
    def plan_only(cls) -> "AgentCapabilities":
        """预设：只规划不动手（只读工具）"""
        return cls(
            allowed_tools={"read_file", "search_code", "send_message"},
            can_execute_actions=False,
            max_tool_risk=ToolRisk.LOW,
        )

class Agent:
    """
    每个 agent 的核心循环: observe → think → act → reflect
    """
    id: str
    role: str
    persona: str
    skills: list[Skill]
    tools: list[Tool]
    provider: LLMProvider
    capabilities: AgentCapabilities    # ← 唯一的能力来源
    # 注：tenant_id 通过 SecurityContext 隐式传递（见 §8.4）

    async def loop(self):
        """主循环（具体 emit 调用见 §5.4）"""
        while self.active:
            observations = await self._observe()
            action = await self._think(observations)
            # 执行前一律走 capability check：
            #   self.capabilities.allowed_tools / max_tool_risk
            # 与 SecurityPolicy.check_tool 协同：capabilities 是 agent 的"先天能力"，
            # SecurityPolicy 是"后天约束"（路径白名单、命令黑名单等）
            result = await self._act(action)
            await self._reflect(result)
```

### 7.2 Agent 状态机

```
                    ┌─────────┐
                    │  idle   │
                    └────┬────┘
                         │ claim task / receive message
                         ▼
                    ┌─────────┐
                    │thinking │◄──────────────┐
                    └────┬────┘               │
                         │ LLM response       │
                         ▼                    │
                    ┌─────────┐     ┌─────────┴──┐
                    │ acting  │────►│ reflecting │
                    └────┬────┘     └─────────┬──┘
                         │                    │
                         │ task done /        │ continue
                         │ no more work       │
                         ▼                    │
                    ┌─────────┐               │
                    │ waiting │───────────────┘
                    └─────────┘  (received message / new task)
```

### 7.3 工具来源：内置工具 + MCP（Model Context Protocol）

> v4 新增：明确表态支持 MCP，开放问题 #3 关闭。

Agent 的工具来自三种来源，统一接入 `Tool` 抽象后无差别使用：

| 来源 | 用途 | 示例 |
|------|------|------|
| **内置工具** | 框架核心能力 | `read_file`, `write_file`, `run_command`, `grep_code` |
| **Skill 自带工具** | 技能模块带的特定工具 | `code-review:security` 自带 `find_sql_injection` |
| **MCP 服务器** | 外部生态工具集成 | GitHub MCP / Jira MCP / 数据库 MCP / 客户私有 MCP |

```python
class MCPToolAdapter(Tool):
    """
    将 MCP 服务器暴露的工具适配为框架内的 Tool

    设计要点:
    - 启动时连接 MCP server（stdio 或 SSE），拉取 tool schema
    - 每个 MCP tool 自动获得一个 ToolRisk 评估（默认 MEDIUM，可配置覆写）
    - 调用时通过 SecurityPolicy 检查（与内置工具一致）
    - MCP 工具的输出大小同样受 TokenBudgetManager.limit_tool_result 约束
    """
    server_name: str
    mcp_tool_name: str
    schema: dict
    risk: ToolRisk = ToolRisk.MEDIUM

class MCPRegistry:
    """MCP 服务器注册表"""
    async def register_server(self, name: str,
                               transport: Literal["stdio", "sse"],
                               config: dict) -> None: ...
    async def list_tools(self) -> list[MCPToolAdapter]: ...
    async def shutdown(self) -> None: ...
```

**配置示例**（`swarm.yaml`）：
```yaml
mcp_servers:
  github:
    transport: stdio
    command: npx
    args: ["-y", "@modelcontextprotocol/server-github"]
    env:
      GITHUB_TOKEN: "${GITHUB_TOKEN}"        # 走 SecretManager 注入，明文不出现在配置中
    risk_overrides:
      create_issue: HIGH      # 创建 issue 视为高风险，需要审批
    reliability:
      auto_reconnect: true
      max_reconnect_attempts: 5
      circuit_breaker_threshold: 3           # 连续 3 次失败暂停该 server 的 tools

  internal-db:
    transport: sse
    url: "https://mcp.internal/db"
    auth: bearer
    token: "${MCP_DB_TOKEN}"
```

**MCP 可靠性策略**（v4.1 补充）：
- **凭证管理**：MCP server 的 env / token 强制走 `SecretManager`（§8.5），日志自动脱敏，不允许明文写入 yaml
- **连接监控**：`MCPRegistry` 后台任务持续监测 server 健康（stdio 进程存活 / SSE 连接），断连后按 `reliability.max_reconnect_attempts` 自动重连（指数退避）
- **熔断**：连续 `circuit_breaker_threshold` 次工具调用失败 → 该 server 的所有 tools 在 60 秒内被标记不可用，agent 调用直接返回 `ToolUnavailableError`，避免雪崩
- **凭证轮换**：`SecretManager.rotate(key)` 触发 → 相关 MCP server 自动重启并重连

**为什么必须支持**：MCP 已成为 Anthropic 主推、社区快速跟进的工具集成标准。框架内置工具永远写不完，开放 MCP 等于接入整个外部生态。不支持 = 自绝于工具市场。

---

## 8. 安全模型

### 8.1 威胁模型

| 信任边界 | 信任级别 | 说明 |
|---------|---------|------|
| LLM 输出 | **不信任** | LLM 可能产生恶意指令，所有 tool call 需经过安全策略检查 |
| Agent 行为 | **不信任** | Agent 的 action 需要经过沙箱和审批 |
| IM 通道 | **不信任** | 消息可能被伪造，强制签名验证 |
| 用户身份 | **需验证** | IM 用户白名单 / API Key 认证 |
| 文件系统 | **限制访问** | Agent 只能访问工作目录 |
| 其他 tenant | **完全隔离** | tenant_id 行级隔离 |

### 8.2 工具安全与沙箱

```python
class ToolRisk(Enum):
    LOW = "low"          # read_file, search_code —— 无需审批
    MEDIUM = "medium"    # write_file —— 需要路径白名单检查
    HIGH = "high"        # run_command —— 需要沙箱 + 默认审批
    CRITICAL = "critical" # 网络请求、外部 API 调用 —— 必须审批

class SandboxManager:
    """
    命令执行沙箱

    默认策略:
    - Docker/podman 容器隔离（推荐）
    - 降级方案: firejail / bubblewrap
    - 开发环境: 工作目录限制 + 命令白名单
    """

    async def execute(
        self,
        command: str,
        workdir: str,
        timeout: float = 30.0,
        max_output_bytes: int = 10240,      # 限制输出 10KB
    ) -> SandboxResult: ...

class SecurityPolicy:
    """
    安全策略引擎

    默认策略（Secure by Default）:
    - run_command: 默认审批 + 沙箱（HIGH）
    - write_file: 路径白名单（MEDIUM）
    - read_file: 敏感路径黑名单（LOW）
    - 所有工具: 速率限制
    """

    def check_tool(self, tool: Tool, agent: Agent, params: dict) -> PolicyDecision:
        """返回: ALLOW / DENY / REQUIRE_APPROVAL"""

    # 敏感路径黑名单
    SENSITIVE_PATHS = [
        "/etc/passwd", "/etc/shadow", "~/.ssh/", "~/.aws/",
        ".env", ".env.local", "credentials", "secrets",
        "/proc/", "/sys/",
    ]

    # 写入路径白名单
    WRITABLE_ROOTS = ["{workspace}/", "{temp}/"]

    # 命令黑名单模式
    COMMAND_BLACKLIST = [
        "rm -rf /", "mkfs.", "dd if=", ":(){ :|:& };:",
        "curl.*|.*bash", "wget.*|.*sh",
    ]
```

### 8.3 Human-in-the-Loop 审批流

```python
class ApprovalFlow:
    """
    人机协作审批流

    默认安全原则:
    - 高风险工具（run_command）默认要求审批
    - 审批超时默认拒绝（fail-closed）
    - 审批通过后重新检查任务状态（乐观锁）
    - 所有审批操作记录审计日志

    v4.1 修订：注册回调时自动捕获当前 SecurityContext，
    触发时自动 scope 进入——异步路径上下文不丢失。
    """

    async def request_approval(
        self,
        agent_id: str,
        request: ApprovalRequest,
        channel: ChannelType,
        timeout_seconds: float = 3600.0,
    ) -> ApprovalResult:
        """
        审批流程：
        1. agent 发起审批 → 生成 ApprovalRequest（自动从 SecurityContextManager.current() 取 session_id/tenant_id）
        2. 通过 ChannelAdapter 发送审批卡片
        3. 用户点击"批准"/"拒绝" → 验证签名
        4. 审批通过后重新检查任务版本号（乐观锁）
        5. 状态一致 → 执行；不一致 → 拒绝并通知
        6. 超时 → 默认拒绝
        7. 完整审计日志记录
        """

    async def register_approval_callback(
        self,
        approval_id: str,
        agent_id: str,
        callback: Callable[[ApprovalResult], Awaitable[None]],
    ) -> None:
        """
        注册审批回调——内部自动捕获当前 SecurityContext。

        实现要点（伪代码）：
            captured_ctx = SecurityContextManager.current()
            self._callbacks[approval_id] = (callback, captured_ctx)

        触发时：
            cb, ctx = self._callbacks.pop(approval_id)
            async with SecurityContextManager.async_scope(ctx):
                await cb(result)

        这保证了即使审批回调跨越了 task 边界（contextvar 已丢失），
        回调内部仍能通过 SecurityContextManager.current() 拿到正确的 ctx。
        """
```

### 8.4 多租户隔离（SecurityContext 模式）

> v4 修订：v3 将 `tenant_id` 写进所有领域对象（Task / Message / Agent / Session）和所有 API 签名，污染严重且难以演进。
> v4 改用 **SecurityContext + contextvars** 隐式传递，领域对象保持纯净；**存储层仍按列/前缀隔离不变**。
> v4.1 修订：补全 `scope()` 的 `@contextmanager` 装饰器，区分同步/异步两个版本；删除半成品的 `capabilities_override` 字段。

#### 8.4.1 设计

```python
import contextvars
from contextlib import contextmanager, asynccontextmanager
from dataclasses import dataclass

@dataclass(frozen=True)
class SecurityContext:
    """
    请求上下文——通过 contextvars 在 async 任务树中隐式传递

    v4.1 修订：删除 capabilities_override（半成品字段，无明确语义和使用场景）
    Agent 的能力始终来自 Agent.capabilities（§7.1），不存在运行时 override。
    """
    tenant_id: str
    user: ChannelUser | None         # 触发本次请求的用户（可能为 None：内部任务）
    session_id: str
    request_id: str                  # 用于日志关联

# 全局 context var
_current_security_ctx: contextvars.ContextVar[SecurityContext] = \
    contextvars.ContextVar("security_ctx")

class SecurityContextManager:
    """
    SecurityContext 访问门面

    v4.1 修订：
    - scope() 改为 @contextmanager 装饰的同步上下文管理器
    - 新增 async_scope()，@asynccontextmanager 装饰，用于异步路径
    """

    @staticmethod
    def current() -> SecurityContext:
        """获取当前上下文——任何代码路径都可用；未设置时抛 LookupError"""
        return _current_security_ctx.get()

    @staticmethod
    @contextmanager
    def scope(ctx: SecurityContext):
        """
        同步 scope。用法：
            with SecurityContextManager.scope(ctx):
                ...
        """
        token = _current_security_ctx.set(ctx)
        try:
            yield
        finally:
            _current_security_ctx.reset(token)

    @staticmethod
    @asynccontextmanager
    async def async_scope(ctx: SecurityContext):
        """
        异步 scope。用法：
            async with SecurityContextManager.async_scope(ctx):
                ...

        在异步回调（审批回调、定时任务、跨 task 边界恢复）中必须使用此版本，
        以保证 contextvar 在 async 边界正确传播。
        """
        token = _current_security_ctx.set(ctx)
        try:
            yield
        finally:
            _current_security_ctx.reset(token)
```

#### 8.4.2 调用链一览

```
ChannelAdapter.route_message(msg)
    │
    │  ① 鉴权 → 解析出 tenant_id + user
    │  ② 创建 SecurityContext
    │  ③ with SecurityContextManager.scope(ctx):
    ▼
Swarm.handle_external_message(msg)
    │
    │  上下文已就绪，无需在签名中传 tenant_id
    ▼
TaskQueue.add(task)
    │
    │  ④ 在 SQL 拼接时取 ctx.tenant_id 作为隔离条件
    ▼
StorageBackend.save_task(task)
    └─ INSERT INTO tasks (tenant_id, ...) VALUES (ctx.tenant_id, ...)
```

#### 8.4.3 存储层依然按租户隔离（不变）

| 后端 | 隔离实现 |
|------|---------|
| **SQLite** | 所有表保留 `tenant_id` 列；StorageBackend 在每条 SQL 自动注入 `WHERE tenant_id = ctx.tenant_id` |
| **Redis** | key 前缀 `{tenant_id}:swarm:{swarm_id}:...`，前缀由 StorageBackend 自动拼接 |
| **File** | 路径 `{storage_dir}/{tenant_id}/...`，由 StorageBackend 拼接 |

**关键差别**：领域对象（Task / Message / Agent）**不再带 tenant_id 字段**；存储层在落盘前从 `SecurityContextManager.current()` 取值注入。

#### 8.4.4 资源配额

```python
class TenantQuota:
    """资源配额（基于 SecurityContextManager.current().tenant_id）"""
    max_swarms_per_tenant: int = 10
    max_agents_per_swarm: int = 10
    max_concurrent_tasks: int = 20
    max_tokens_per_day: int = 10_000_000

    async def check_and_consume(self, resource: str, amount: int = 1) -> bool: ...
```

#### 8.4.5 越权防护（Defense in Depth）

- **API 层**：`ChannelAdapter` 鉴权后才创建 SecurityContext，否则拒绝请求
- **存储层**：所有查询强制注入 `WHERE tenant_id = ?`；忘记注入 → 单元测试拦截（专门的 lint 规则检查 SQL）
- **回调层**：审批回调、定时任务等异步路径必须显式 `async with SecurityContextManager.async_scope(ctx)` 才能执行——避免 contextvar 在 task 边界丢失（ApprovalFlow 已在 `register_approval_callback` 内部自动捕获并恢复，见 §8.3）

#### 8.4.6 单租户模式（默认）

```yaml
security:
  tenant:
    mode: single                # single | multi
    default_tenant_id: local    # 单租户模式下使用的固定 tenant_id
```

单租户模式下，框架自动注入 `SecurityContext(tenant_id="local", ...)`，开发者无需感知。从单租户切到多租户**零 schema 变更**——存储层早就按 tenant_id 隔离了。

### 8.5 密钥管理

```python
class SecretManager:
    """
    密钥管理

    原则:
    - 所有密钥通过环境变量或 Vault 注入，不写在配置文件中
    - 日志自动脱敏（app_secret → "lark_***"）
    - 内存中密钥不可通过 debug 接口暴露
    - 支持密钥轮换通知
    """

    def get(self, key: str) -> str: ...
    def mask_in_logs(self, message: str) -> str: ...
```

---

## 9. LLM Provider 层

### 9.1 统一抽象

```python
class LLMProvider(ABC):
    """LLM 后端统一接口"""

    @abstractmethod
    async def chat(self, messages: list[dict],
                   tools: list[ToolSchema] = None,
                   stream: bool = True, **kwargs) -> LLMResponse: ...

    @abstractmethod
    async def chat_stream(self, messages: list[dict],
                          tools: list[ToolSchema] = None,
                          **kwargs) -> AsyncIterator[LLMChunk]: ...

    @abstractmethod
    def count_tokens(self, messages: list[dict]) -> int: ...

    @property
    @abstractmethod
    def context_window(self) -> int: ...

    @property
    @abstractmethod
    def pricing(self) -> PricingInfo: ...

    @property
    def supports_embedding(self) -> bool:
        """是否支持 embedding API（语义缓存需要）"""
        return False
```

### 9.2 支持的 Provider

| Provider | 模型示例 | 特性 |
|----------|---------|------|
| OpenAI | gpt-4o, gpt-4-turbo | 函数调用、流式、embedding |
| Anthropic | claude-opus-4-8, claude-sonnet-4-6 | 长上下文、tool use |
| DeepSeek | deepseek-v4 | 高性价比推理 |
| Ollama | llama3, mistral 等 | 本地运行、离线 |
| Groq | llama-3.1-70b | 低延迟推理 |

### 9.3 Token 预算管理

```python
class TokenBudgetManager:
    """
    主动 token 预算管理——防止 LLM 调用因 token 超限而失败

    在每次 LLM 调用前主动计算预算：
    used = count(system_prompt) + count(messages) + count(tool_schemas) + reserve
    如果 used > context_window * 0.8，先截断再调用
    """

    def __init__(self, context_window: int):
        self.context_window = context_window
        self.reserve_tokens = 4096          # 预留给回复
        self.warning_threshold = 0.8        # 80% 触发截断

    def calculate_usage(
        self,
        system_prompt: str,
        messages: list[dict],
        tool_schemas: list[dict] | None,
    ) -> TokenBudget:
        """
        返回当前 token 使用量和剩余预算
        注意：tool call schema 也消耗 token，必须计入
        """

    def smart_truncate(
        self,
        messages: list[dict],
        budget: TokenBudget,
        refs: list[str] = None,
    ) -> list[dict]:
        """
        智能截断策略：
        1. 保留系统 prompt（不可截）
        2. 保留最近的 N 条消息
        3. 保留被 @ref 引用的历史消息
        4. 当历史超过 context_window * 0.5 时主动生成滑动窗口摘要
        """

    async def generate_summary(self, messages: list[dict]) -> str:
        """用轻量模型（gpt-4o-mini / claude-haiku）做摘要"""

    def limit_tool_result(self, result: str, max_chars: int = 10000) -> str:
        """
        限制工具返回内容大小：
        - read_file: 限制 500 行
        - run_command: 限制 10KB 输出
        - 超大结果写临时文件，只给 agent 一个摘要
        """
```

**Token 消耗估算公式**：

```
total_tokens ≈ agents × avg_turns × (system_prompt + context + tool_results)

典型场景参考值（Claude Sonnet，code review）:
- 3 agent × 5 turns × (2K + 8K + 3K) = 195K tokens ≈ $0.60
- 5 agent × 5 turns（对抗式验证）× (2K + 12K + 5K) = 475K tokens ≈ $1.45
```

### 9.4 LLM 调用缓存

```python
class LLMCache:
    """多层缓存策略（MVP: L1 + L3，L2 语义缓存远期）"""

    # L1: 精确匹配（相同 prompt + 相同上下文 → 直接返回）
    exact_cache: LRUCache[str, LLMResponse]

    # L3: prompt 模板缓存（相同模板不同参数 → 只算增量）
    template_cache: TemplateCache
```

---

## 10. 存储后端

> v4 修订：
> - 所有 API 签名移除 `tenant_id` 参数 → 由 `SecurityContextManager.current()` 隐式提供（见 §8.4）
> - In-Memory 后端删除 → 改为 `SQLiteBackend(":memory:")` 别名（同一份代码，零维护成本）

### 10.1 可插拔设计

```python
class StorageBackend(ABC):
    """存储后端统一接口（tenant_id 由 SecurityContext 隐式提供）"""

    # === 任务 ===
    @abstractmethod
    async def save_task(self, task: Task) -> None: ...
    @abstractmethod
    async def get_task(self, task_id: str) -> Task | None: ...
    @abstractmethod
    async def list_tasks(self, status: str | None = None,
                          limit: int = 100, offset: int = 0) -> list[Task]: ...
    @abstractmethod
    async def cas_update_task(self, task_id: str, expected_version: int,
                                updates: dict) -> Task | None:
        """CAS 更新——TaskQueue 的 claim/complete/fail 全部走这里"""

    # === 消息 ===
    @abstractmethod
    async def save_message(self, msg: Message) -> None: ...
    @abstractmethod
    async def get_messages(self, agent_id: str,
                            since: float | None = None) -> list[Message]: ...
    @abstractmethod
    async def get_messages_batch(
        self, agent_ids: list[str]
    ) -> dict[str, list[Message]]: ...

    # === 事件（来自 ObservabilityBus 的 SqliteEventSink） ===
    @abstractmethod
    async def save_event(self, event: SessionEvent) -> None: ...
    @abstractmethod
    async def save_events_batch(self, events: list[SessionEvent]) -> None: ...
    @abstractmethod
    async def get_events(self, session_id: str) -> list[SessionEvent]: ...

    # === 事务 ===
    @abstractmethod
    async def transaction(self) -> "TransactionContext": ...
```

> v4.1 修订：MVP ABC 中删除 `acquire_lock` / `release_lock`——任务认领走 CAS（§6.4），MVP 阶段无其他互斥需求。如果远期出现明确用例，作为可选 mixin 加入。

### 10.2 后端选择

| 后端 | 适用场景 | CAS 实现 | 并发限制 |
|------|---------|--------|---------|
| **SQLite (file)** (默认) | 单机 < 5 agent | `UPDATE ... WHERE version=?` (返回 rowcount) | 1 writer，读并发 |
| **SQLite (`:memory:`)** | 单元测试 / 演示 | 同上 | 单进程 |
| **Redis** | 生产 / 10+ agent / 分布式 | `WATCH/MULTI/EXEC` 或 Lua 脚本 | 高并发 |
| **File** | 零依赖兼容（不推荐，仅作降级） | fcntl + 重写 | 单机 |

> v3 的独立 `InMemoryBackend` 被删除——`SQLiteBackend(":memory:")` 已经覆盖测试场景，重复实现纯属维护负担。

**File 后端可靠性**（保持 v3 警告）：
- `fcntl.flock` 在 NFS 上不可靠
- File 后端只适用于单机本地文件系统；容器/NFS 必须用 Redis
- 框架检测到 NFS 挂载点时发出警告

### 10.3 租户命名空间

存储层从 `SecurityContextManager.current().tenant_id` 自动取值注入：

```
SQLite: WHERE tenant_id = ? 由 StorageBackend 自动添加
Redis:  key 前缀 {tenant_id}:swarm:{swarm_id}:... 由 StorageBackend 自动拼接
File:   路径 {storage_dir}/{tenant_id}/... 由 StorageBackend 自动拼接
```

调用方（TaskQueue / Mailbox / SessionManager）**完全感知不到 tenant_id**——这是 §8.4 SecurityContext 模式的核心收益。

---

## 11. 内置技能系统

### 11.1 技能定义

```python
class Skill:
    """可复用的能力模块"""
    name: str
    description: str
    version: str
    category: Literal["review", "debug", "develop", "analyze", "ops"]

    system_prompt_extension: str
    tools: list[Tool]
    recommended_model: str | None

    async def validate_input(self, context: dict) -> bool: ...
    async def validate_output(self, result: Any) -> bool: ...
```

### 11.2 内置技能库（MVP 11 个）

| 类别 | 技能 ID | 描述 | 推荐模型 |
|------|--------|------|---------|
| **审查** | `code-review:security` | SQL 注入、XSS、认证缺陷检测 | - |
| | `code-review:performance` | N+1 查询、内存泄漏、算法复杂度 | - |
| | `code-review:architecture` | 模块耦合、SOLID、设计模式 | - |
| **调试** | `debug:root-cause` | 从现象出发，假设→证据→验证 | Opus |
| | `debug:adversarial` | 多 agent 互相质疑找到真相 | Opus |
| **开发** | `develop:api-endpoint` | OpenAPI → 完整端点代码+测试 | - |
| | `develop:db-migration` | 安全迁移脚本+回滚方案 | - |
| | `develop:refactor` | 提取方法、消除重复、改善命名 | - |
| **分析** | `analyze:complexity` | 圈复杂度、认知复杂度、依赖图 | - |
| | `analyze:test-gap` | 未覆盖的代码路径分析 | - |
| **运维** | `ops:oncall` | 告警排查：日志、指标、变更 | - |

> 技能市场/在线仓库为远期规划，MVP 阶段内置技能即可。

---

## 12. 高性能策略

### 12.1 异步优先

```python
class AgentRuntime:
    """全异步 agent 运行时"""

    _http_session: aiohttp.ClientSession
    _llm_semaphore: asyncio.Semaphore   # 默认 = provider rate limit * 0.8
    _file_executor: ThreadPoolExecutor

    async def run_agents_parallel(self, agents: list[Agent]):
        async with asyncio.TaskGroup() as tg:
            for agent in agents:
                tg.create_task(agent.loop())
```

### 12.2 连接池与并发控制

```python
class ConnectionManager:
    """
    LLM API 连接管理

    并发控制:
    - _llm_semaphore 默认值 = provider rate limit 的 80%
    - 如 OpenAI GPT-4o 限 500 req/min → semaphore = 400
    - 每个 provider 独立的 rate limiter 和 circuit breaker
    """

    _pools: dict[str, Pool]
    _rate_limiters: dict[str, RateLimiter]       # 按 provider 分桶
    _retry_config: RetryConfig                    # 指数退避 + jitter
    _circuit_breakers: dict[str, CircuitBreaker]  # 连续失败 N 次暂停
```

### 12.3 流式处理

```python
class StreamingProcessor:
    """
    LLM 响应流式处理

    飞书通道流式推送策略:
    - 每积累 50 个 token 或遇到换行 → 推送一次
    - 避免逐 token 推送（消息风暴），也避免全部完成才推（用户等太久）
    """

    async def process_stream(
        self,
        stream: AsyncIterator[Chunk],
        on_text: Callable[[str], Awaitable[None]],
        on_tool_call: Callable[[ToolCall], Awaitable[None]],
        flush_interval_tokens: int = 50,
    ) -> LLMResponse: ...
```

### 12.4 SQLite 性能优化

```
WAL 模式配置:
  PRAGMA journal_mode=WAL;
  PRAGMA wal_autocheckpoint=1000;   -- 被动 checkpoint
  PRAGMA synchronous=NORMAL;        -- 平衡安全与性能
  PRAGMA cache_size=-64000;         -- 64MB 缓存

批量写入:
  - Session events 批量 INSERT，而非逐条写入
  - 每 100 条或每 5 秒刷一次盘

适用规模:
  - SQLite 适用于 < 5 agent 并发
  - 10+ agent 推荐切换到 Redis
```

---

## 13. 项目目录结构

```
agent-swarm/
├── pyproject.toml
├── README.md
├── DESIGN.md
├── src/
│   └── agent_swarm/
│       ├── __init__.py
│       │
│       ├── core/                      # 编排与核心模块
│       │   ├── __init__.py
│       │   ├── swarm.py              # Swarm 编排器（总入口）
│       │   ├── agent.py              # Agent 基类 + 异步生命周期
│       │   ├── task_queue.py         # Task Queue
│       │   ├── mailbox.py            # 点对点消息系统
│       │   ├── knowledge_base.py     # 共享知识管理
│       │   ├── conversation_context.py # 隔离上下文管理
│       │   └── session.py            # 会话管理（持久化 + 恢复）
│       │
│       ├── protocols/                # 协作协议
│       │   ├── __init__.py
│       │   ├── base.py               # Protocol 基类
│       │   ├── delegate.py           # 委托模式
│       │   └── adversarial.py        # 对抗式验证
│       │
│       ├── security/                 # 安全模块
│       │   ├── __init__.py
│       │   ├── policy.py             # SecurityPolicy 引擎
│       │   ├── sandbox.py            # SandboxManager
│       │   ├── approval.py           # ApprovalFlow（含回调 ctx 自动捕获）
│       │   ├── security_context.py   # SecurityContext + Manager + TenantQuota（v4.1：原 tenant.py）
│       │   └── secrets.py            # SecretManager
│       │
│       ├── skills/                   # 内置技能库
│       │   ├── __init__.py
│       │   ├── base.py
│       │   ├── review.py
│       │   ├── debug.py
│       │   ├── develop.py
│       │   ├── analyze.py
│       │   └── ops.py
│       │
│       ├── providers/                # LLM 适配层
│       │   ├── __init__.py
│       │   ├── base.py
│       │   ├── openai.py
│       │   ├── anthropic.py
│       │   ├── deepseek.py
│       │   ├── token_budget.py       # TokenBudgetManager
│       │   └── cache.py              # LLMCache
│       │
│       ├── observability/            # 可观测（横切面）
│       │   ├── __init__.py
│       │   ├── bus.py
│       │   ├── metrics.py
│       │   ├── logging.py
│       │   └── session_store.py
│       │
│       ├── transport/                # 存储后端
│       │   ├── __init__.py
│       │   ├── base.py
│       │   ├── sqlite.py             # 文件 + :memory: 共用此实现
│       │   ├── redis.py
│       │   └── file.py
│       │
│       ├── tools/                    # Agent 工具
│       │   ├── __init__.py
│       │   ├── base.py
│       │   ├── risk.py               # ToolRisk 等级定义
│       │   ├── mcp/                  # MCP 工具集成（v4 新增）
│       │   │   ├── __init__.py
│       │   │   ├── adapter.py        # MCPToolAdapter
│       │   │   ├── registry.py       # MCPRegistry
│       │   │   └── transports.py     # stdio / sse 传输
│       │   └── builtin/
│       │       ├── __init__.py
│       │       ├── file_ops.py
│       │       ├── shell.py
│       │       ├── web.py
│       │       └── search.py
│       │
│       ├── channels/                 # 消息通道层
│       │   ├── __init__.py
│       │   ├── base.py
│       │   ├── adapter.py
│       │   ├── router.py
│       │   ├── session_binding.py
│       │   ├── lark.py               # 飞书连接器（MVP）
│       │   └── cards/                # 卡片模板
│       │       ├── __init__.py
│       │       ├── task_progress.json
│       │       ├── code_review_result.json
│       │       ├── adversarial_debug.json
│       │       ├── swarm_status.json
│       │       └── confirm_dialog.json
│       │
│       └── cli/                      # CLI 交互层
│           ├── __init__.py
│           ├── main.py
│           ├── tui.py
│           └── commands.py
│
├── examples/
│   ├── 01_code_review.py
│   ├── 02_adversarial_debug.py
│   ├── 03_cross_layer_dev.py
│   ├── 04_lark_oncall.py             # 飞书值班排查示例
│   ├── w1_hello.yaml                 # Weekly Slice W1 演示
│   ├── w2_two_agents.yaml            # ...
│   └── dogfood_pr_review.yaml        # 自审 PR（§17.7）
│
├── tests/
│   ├── conftest.py                   # 含 LLM mock fixture（§17.4）
│   ├── unit/                         # 70%——纯单元，零 IO
│   ├── integration/                  # 25%——mock LLM，真实其他组件
│   ├── e2e/                          # Weekly Slice DoD 测试
│   ├── golden/                       # §17.3 验收场景库
│   │   ├── cases/
│   │   │   ├── G-001_pr_security_review/
│   │   │   │   ├── input.yaml
│   │   │   │   ├── expected.yaml
│   │   │   │   └── README.md
│   │   │   └── ...
│   │   └── baseline.yaml             # §17.5 性能基线
│   ├── security/                     # 安全攻击测试套件
│   ├── _recordings/                  # LLM 录制回放
│   └── ...
│
├── tools/
│   ├── benchmark.py                  # §17.5 周度性能基线
│   ├── check_sql_lint.py             # 强制所有 SQL 注入 WHERE tenant_id
│   └── lint_tests.py                 # 测试反模式检测
│
├── demos/                            # §17.1 Weekly Slice 演示录屏
│   ├── wk1-hello.mp4
│   └── ...
│
├── docs/                             # §17.7 文档交付
│   ├── concepts.md
│   ├── recipes/
│   ├── troubleshooting.md
│   └── api/
│
├── .github/
│   └── workflows/
│       ├── ci.yml                    # §17.4 PR 阻塞检查
│       └── nightly.yml               # 真实 LLM + benchmark
│
└── config/
    └── default_swarm.yaml
```

---

## 14. 使用示例

### 14.1 YAML 配置

```yaml
# swarm.yaml
name: code-review-team
description: PR 代码审查团队

channels:
  lark:
    enabled: true
    app_id: "cli_xxxxxxxx"
    app_secret: "${LARK_APP_SECRET}"
    verification_token: "${LARK_VERIFICATION_TOKEN}"
    user_whitelist:
      - "ou_abc123"

observability:
  metrics: true
  session_store: sqlite://./sessions.db
  log_level: info

agents:
  - id: sec-1
    role: 安全专家
    provider: anthropic
    model: claude-sonnet-4-6
    skills: [code-review:security, debug:adversarial]
    tools: [read_file, grep_code]
    # run_command 默认需要审批（HIGH risk），未在 tools 中则不授予

  - id: perf-1
    role: 性能分析师
    provider: openai
    model: gpt-4o
    skills: [code-review:performance, analyze:complexity]
    tools: [read_file]

  - id: lead-1
    role: Team Lead
    provider: anthropic
    model: claude-opus-4-8
    capabilities: lead              # ← v4：使用 capabilities 预设替代 v3 的 mode

tasks:
  - title: 安全审查 PR #142
    assigned_skill: code-review:security
  - title: 性能审查 PR #142
    assigned_skill: code-review:performance
  - title: 汇总审查报告
    assigned_to: lead-1
    depends_on: [安全审查 PR #142, 性能审查 PR #142]

security:
  approval:
    enabled: true                      # 默认开启
    require_for: [run_command]         # 高风险工具默认审批
    timeout: 3600                      # 超时默认拒绝
  sandbox:
    mode: docker                       # docker | firejail | workspace_only
  tenant:
    mode: single                       # single（默认）| multi
    default_tenant_id: local           # single 模式下使用的固定 tenant_id
```

### 14.2 Python SDK 示例

```python
from agent_swarm import Swarm

swarm = Swarm.from_yaml("swarm.yaml")
await swarm.run()
print(swarm.status())
```

### 14.3 飞书值班排查示例

```python
from agent_swarm import Swarm
from agent_swarm.channels import LarkConnector, ChannelAdapter

swarm = Swarm.from_yaml("oncall-swarm.yaml")

adapter = ChannelAdapter()
lark = LarkConnector(
    app_id="cli_xxx",
    app_secret=SecretManager.get("LARK_APP_SECRET"),
    verification_token=SecretManager.get("LARK_VERIFICATION_TOKEN"),
    user_whitelist=["ou_oncall_team"],
)
adapter.register_connector(lark)

await adapter.start()
await swarm.run()
# 飞书群里 @机器人 "生产环境 5% 请求 500，帮我排查"
```

### 14.4 对抗式调试模板

```python
from agent_swarm.protocols import AdversarialVerifier

verifier = AdversarialVerifier(max_rounds=5)

verdict = await verifier.verify(
    hypotheses=[
        "数据库连接池在高负载下耗尽",
        "库存预留中的竞态条件",
        "第三方支付 API 超时处理",
        "内存压力导致 GC 暂停",
        "服务间网络问题",
    ],
    agents=[
        Agent(id="db-1", role="数据库专家", skills=["debug:root-cause"]),
        Agent(id="race-1", role="并发专家", skills=["debug:root-cause"]),
        Agent(id="api-1", role="API 专家", skills=["debug:root-cause"]),
        Agent(id="mem-1", role="内存专家", skills=["debug:root-cause"]),
        Agent(id="net-1", role="网络专家", skills=["debug:root-cause"]),
    ],
    min_survivors=1,
    max_rounds=5,      # 防止无限循环
)

print(f"根因: {verdict.root_cause}, 置信度: {verdict.confidence}")
```

---

## 15. MVP 分阶段计划

> v4.1：把 SecurityContext 注入 Phase 1。
> v4.2：**Phase 1 重排为 6 周垂直切片（Weekly Slice），每周末必须有端到端可跑的演示**——避免"水平切片堆叠到 99% 才发现跑不起来"陷阱。

### Phase 1: 核心骨架 — 6 周垂直切片

**核心原则**：每周完成一个**端到端可演示**的窄纵向切片，下一周在其之上叠加新维度。任何一周末跑不通，立即停止下周计划，回头修。

> **关于 SecurityContext 的时间线澄清（v4.2）**：本文档前面提到"Phase 1 必须有 SecurityContext 基建"——这个基建在 W5 才**正式落地为抽象层**（SecurityContextManager + contextvars + 存储层 SQL 自动注入）。W1-W4 期间，所有路径硬编码使用 `tenant_id="local"`（单租户默认），保持代码可演进的同时不引入抽象成本。这是垂直切片节奏的必要妥协——不阻塞 W1 跑通最短链路。

| 周次 | 演示目标（DoD） | 新增维度 | 沿用上周 |
|------|----------------|---------|---------|
| **W1** | `agent-swarm run hello.yaml` 启动单 agent，读 README 并总结，CLI 打印结果 | Agent loop 骨架 / 内存存储 / OpenAI Provider / 1 个工具 (`read_file`) / 最简 CLI | — |
| **W2** | 双 agent 通过 Mailbox 协作，A 读文件 → 发消息给 B → B 写总结。Task Queue 管理依赖 | Mailbox / TaskQueue（CAS）/ 第二个 Provider (Anthropic) | W1 |
| **W3** | 中断后 `agent-swarm session resume <id>` 恢复继续，对话历史重建一致 | SQLite 存储 / SessionManager / ObservabilityBus + SqliteEventSink | W2 |
| **W4** | 跑 §17.3 的 Golden Case "PR 安全审查"全流程，结果写入 KB 缓存供后续复用 | KnowledgeBase + ConversationContext (含 external_inputs) / Skill 系统 / 1 个内置技能 (`code-review:security`) | W3 |
| **W5** | 启用 SecurityContext + workspace_only 沙箱 + Token Budget；恶意 prompt 注入测试无法越权读 `/etc/passwd` | SecurityContext / SecurityPolicy / SandboxManager / TokenBudgetManager | W4 |
| **W6** | TUI 仪表盘实时显示 swarm 状态、任务进度、消息流；Phase 1 全部 DoD（§17.2）通过 | TUI (Textual) / SwarmStatus 完整接入 / 文档 + 示例固化 | W5 |

> 每周演示的录屏 + 对应 git tag (`w1-demo`...`w6-demo`) 作为里程碑证据。

### Phase 2: 协作与通道（6-8 周）

| 模块 | 内容 |
|------|------|
| Delegate Mode | Lead + Worker 分离（基于 AgentCapabilities） |
| Adversarial Verify | 对抗式验证（含 Stance/Judgement/HypothesisState 完整算法）+ Golden Case 验收 |
| MCP 工具集成 | MCPRegistry + MCPToolAdapter（stdio + SSE）+ 重连/熔断 |
| 飞书连接器 | 应用模式 + 卡片交互 |
| Approval Flow | Human-in-the-Loop 审批 + 自动捕获 SecurityContext |
| 内置技能 | 11 个内置技能 |
| 可观测加强 | WebSocketSink（推送到 TUI）+ 完整事件目录落地 |
| **Dogfooding 启动** | Phase 2 末期：用 agent-swarm 自审本项目 PR（见 §17.7） |

### Phase 3: 生产加固（8-12 周）

| 模块 | 内容 |
|------|------|
| 多租户开放 | 把 Phase 1 已就绪的 SecurityContext 多租户能力开放：`mode: multi` 配置、TenantQuota 配额生效、跨租户隔离压测 |
| Docker Sandbox | 命令执行容器隔离（替换 workspace_only 默认） |
| Redis 后端 | 分布式存储 + CAS（WATCH/MULTI/EXEC 或 Lua） |
| Prometheus 导出 | PrometheusSink |
| 密钥管理 | SecretManager + Vault 集成 + 凭证轮换通知 |

### 远期（按需）

| 模块 | 内容 |
|------|------|
| 微信连接器 | 企业微信 / 公众号 |
| Web GUI Dashboard | 前端管理面板 |
| Pipeline Engine | 声明式流水线 |
| 语义缓存 | LLM 调用语义去重 |
| 技能市场 | 在线仓库 |
| 多通道 Session 合并 | 跨通道用户身份统一 |

---

## 16. 开放问题

### 16.1 v4 已关闭（来自 v3 的开放问题）

| # | 问题 | v4 决议 |
|---|------|------|
| 3 | 是否支持 MCP？ | ✅ **支持**——见 §7.3，作为外部工具来源之一 |
| 7 | 审批超时默认拒绝还是可配置？ | ✅ **默认拒绝**（fail-closed），可配置覆写 |

### 16.2 仍然开放

| # | 问题 | 选项 | 倾向 |
|---|------|------|------|
| 1 | Sandbox 默认模式？ | workspace_only / docker / firejail | MVP: workspace_only；生产: docker |
| 2 | GUI 前端用哪个框架？（远期） | HTMX / Alpine.js / React / Vue | 待 GUI 实做时决策 |
| 4 | 技能市场需要在线仓库吗？（远期） | GitHub 仓库 / PyPI 包 / 内置即可 | 远期再说 |
| 5 | 多语言 SDK？ | Python 优先 / 同步推进 | Python 优先 |
| 6 | Agent 间通信是否需要加密/签名？ | 明文 / 可选 TLS | 进程内默认明文，跨网络可选 TLS |
| 8 | SQLite < 5 agent，是否自动切 Redis？ | 自动检测 / 手动切换 | 手动切换 + 文档建议 |

### 16.3 v4 新引入（待实做时验证）

| # | 问题 | 备注 |
|---|------|------|
| 9 | AdversarialVerifier 的 ELIMINATE_THRESHOLD 默认值（-0.5）是否合理？ | 需要在多个真实案例上压测调参 |
| 10 | MCP server 的 ToolRisk 默认值是否应按 server 来源（官方/社区/私有）分级？ | 待 MCP 生态成熟后回顾 |
| 11 | SecurityContext 在 `asyncio.create_task` 中的丢失风险——是否需要全局拦截器？ | 倾向：约定 + lint 规则；不引入魔法 |

---

## 17. 工程实践与交付门禁

> v4.2 新增。设计契约（前 16 章）回答"做什么"，本章回答"如何确保做对、做完、能跑"。
> 没有这一章，DESIGN.md 只是一份学术 paper；有了这一章，才是工程契约。

### 17.1 垂直切片 MVP（已落地于 §15）

**核心思想**：每周完成一个**端到端可演示**的窄纵向切片，而非"水平堆模块到最后集成"。

**为什么垂直切片**：
- 水平切片：6 周内每个模块完成度 100% → 第 6 周末才能集成 → 集成失败 = 整盘卡住
- 垂直切片：每周末有可跑的完整链路 → 早期暴露所有跨模块问题 → 风险前置

**强制规则**：
1. 每周末必须有 `git tag w<N>-demo` 标记里程碑
2. 每周末必须录制 ≤2 分钟演示视频，命名 `demos/wk<N>-<feature>.mp4`
3. 当周 DoD（§17.2）未通过 → 下周计划顺延，**不允许并行赶工**
4. Slice 之间禁止"假桩"——本周必须真实运行，不能 mock 留待下周替换（mock 会导致下周才发现接口设计有问题）

### 17.2 Definition of Done（量化的"完成"标准）

> 每个 Phase / Weekly Slice 给出**机器可验证**的完成条件。模糊判断（"差不多了"）不算完成。

#### Phase 1 Weekly Slice DoD

| Week | 自动化 DoD（CI 必须全绿才算完成） |
|------|------|
| W1 | `pytest tests/e2e/test_w1_hello.py` 通过；`agent-swarm run examples/w1_hello.yaml` 退出码=0；输出包含 README 关键词 |
| W2 | `tests/e2e/test_w2_two_agents.py` 通过；演示 swarm 含 ≥2 agent，TaskQueue 显示 1→2→1 状态流转；CAS 冲突日志 ≥1 条（证明锁机制工作） |
| W3 | `tests/e2e/test_w3_resume.py` 通过；首次运行后 `kill -9` swarm 进程 → resume 命令在新进程里 100% 重建状态（消息数 / 任务数 / 已完成数完全一致） |
| W4 | `tests/e2e/test_w4_golden_pr_security.py` 通过；§17.3 的 Golden Case G-001 输出含期望的 ≥3 个安全问题；KB 缓存命中率 ≥60% 在第二次运行中 |
| W5 | `tests/security/test_w5_sandbox_escape.py` 通过；20 条 prompt injection / path traversal / command injection 攻击全部被拦截；token 超限场景下能优雅截断不崩溃 |
| W6 | TUI 启动后 5 秒内显示完整 swarm 视图；Phase 1 全部 §17.3 列入 Phase 1 的 Golden Case 通过；`README.md` quickstart 5 分钟可上手 |

#### Phase 2 Weekly Slice DoD

> v4.2 追加：Phase 2 重排为 W7-W12 六个垂直切片（与 Phase 1 节奏一致）。
> 当前状态：W7 完成，W8+ 待办。

| Week | 自动化 DoD（CI 必须全绿才算完成） |
|------|------|
| **W7** | ① `pytest tests/unit/test_types.py tests/unit/test_protocols.py tests/unit/test_swarm_protocol_api.py tests/unit/test_lead_tools.py tests/e2e/test_w7_delegate_e2e.py` 全过 ② `agent-swarm run examples/w7_delegate.yaml` 退出码=0 ③ Swarm.from_yaml 支持 `role_type: lead / worker / plan_only`（默认 worker，向后兼容 Phase 1 全部 examples） ④ lead agent 的 tools 注入 5 个 lead 工具（spawn_agent / shutdown_agent / assign_task / update_task / review_plan）；worker agent 的 tools **不**含 lead 工具 ⑤ DelegateMode 协议校验：swarm 无 lead / 无 worker → `ProtocolResult.success=False` + 明确 error ⑥ `ProtocolResult.artifacts` 含 `leads` / `workers` / `tasks_total` / `tasks_completed` / `tasks_failed` / `swarm_state` 字段 ⑦ `README.md` quickstart 加 W7 入口（CLI + 程序化） |
| **W8** | ① `pytest tests/unit/test_adversarial_types.py tests/unit/test_adversarial_round.py tests/unit/test_adversarial_convergence.py tests/unit/test_adversarial_verifier.py tests/e2e/test_w8_adversarial_e2e.py tests/golden/test_golden_p2.py` 全过 ② `AdversarialVerifier(min_survivors=1, max_rounds=5, eliminate_threshold=-0.5)` 默认参数下能跑通 ③ `examples/w8_adversarial.yaml` 含 3 个 plan_only judge + 3 假设任务，从 YAML 可构造 Swarm ④ 5 个 P2 Golden Case 根因命中率 ≥80%（G-011..G-015） ⑤ AdversarialVerifier 主循环覆盖 4 条收敛路径：`min_survivors_reached` / `consensus_stable` / `max_rounds_exhausted` / `all_eliminated` ⑥ 错误兜底：单轮全员失败 → 该轮作废（rollback）；连续 2 轮 → `VerifierStallError`；单 agent 异常 → 记为 UNCERTAIN（不影响其他 agent） ⑦ `ProtocolResult.artifacts` 含 `protocol` / `survivors` / `eliminated` / `rounds_used` / `convergence_reason` / `root_cause` / `confidence` 字段 ⑧ `README.md` quickstart 加 W8 入口 |
| **W9** | ① `pytest tests/unit/test_mcp_registry.py tests/unit/test_mcp_stdio.py tests/unit/test_mcp_adapter.py tests/e2e/test_w9_mcp_e2e.py` 全过 ② `MCPRegistry.from_dict(cfg)` 可解析 ≥2 server 配置（filesystem + GitHub） ③ `StdioMCPClient` JSON-RPC 2.0 协议：initialize / tools/list / tools/call 走通 + 超时/MCPRPCError/MCPConnectionError 错误处理 ④ `MCPToolAdapter` 把 MCP tool 包装为 agent_swarm Tool（name 加 `mcp.<server>.<tool>` 前缀避免跨 server 冲突） ⑤ `await_build_tool_adapters` 异步工厂：connect + list_tools + 包装；risk_overrides 覆写 ⑥ `examples/w9_mcp_github_filesystem.yaml` 含 2 个 MCP server 配置（filesystem + GitHub）；YAML 合法 + MCPRegistry.from_dict 可消费 ⑦ **Phase 2 DoD ③**：MCP 至少接入 2 个 server 走通——filesystem (stdio) + GitHub (stdio)；e2e 用 mock server 验证 list_tools / call_tool 端到端 ⑧ `README.md` quickstart + 状态表加 W9 入口 ⑨ SSE 传输 / 重连熔断 推迟到 W10+（DESIGN §7.3 提到但 Phase 2 DoD ③ 字面只要求"≥2 server"；stdio 已够） |
| **W10** | ① `pytest tests/unit/test_channels_base.py tests/unit/test_channels_lark.py tests/unit/test_channels_adapter.py tests/unit/test_card_templates.py tests/e2e/test_w10_lark_e2e.py` 全过 ② `LarkConnector` HMAC-SHA256 签名验证：不同 key 必产生不同签名（REVIEW-2026-06-19-2 H1 回归测试） ③ 5 个内置卡片模板（task_progress / code_review_result / adversarial_debug / swarm_status / confirm_dialog）渲染合法 ④ `ChannelAdapter` 路由：注册/鉴权/限流 (RateLimiter sliding window) / session 绑定 ⑤ `examples/w10_lark.yaml` 3 个密钥字段用 SecretManager 引用 (${LARK_APP_SECRET} / ${LARK_VERIFICATION_TOKEN} / ${LARK_ENCRYPT_KEY}) ⑥ `README.md` quickstart + 状态表加 W10 入口 ⑦ 飞书真实工作区接入：可配置 + mock 默认开启 ⑧ Decrypt：encrypt_key 启用时走真 AES-256-CBC（cryptography 可选依赖，lazy import） |
| **W11** | ① `pytest tests/unit/test_channel_approver.py tests/e2e/test_w11_approval_e2e.py` 全过 ② `ChannelApprover` 异步等待用户回复 + 超时 fail-closed ③ `ApprovalFlow.request_approval` 升级为 async；`RunCommandTool` / `MCPToolAdapter` 走 `await approval_flow.request_approval` ④ `ChannelAdapter` 注册为 approver，`REQUIRE_APPROVAL` 决策通过飞书 confirm_dialog 卡片异步等待 ⑤ 飞书真实工作区：approve / deny / timeout 三种回调路径都验证 ⑥ `tools/verify_w11_dod.py` 5/5 通过 |
| **W12** | ① `pytest tests/unit/test_websocket_sink.py tests/e2e/test_w12_websocket_e2e.py` 全过 ② `WebSocketSink` aiohttp server + 多客户端 fan-out + 心跳（ping/pong）+ 断线重连 ③ 背压：单客户端 max_queue=256 满 → 丢最旧 + `dropped_events` 计数；`_send_loop` 慢消费者不阻塞 `consume()` ④ **完整事件目录**（SqliteEventSink）：5 元组索引 `idx_events_5tuple(session_id, tenant_id, event_name, seq, request_id)` + 时间索引 `idx_events_tenant_time` + request_id 索引 `idx_events_request_id` ⑤ TUI 实时仪表盘接入（`tools/verify_w12_dod.py` 5/5 通过）⑥ Prometheus 指标基础：`event_count` + `duration_seconds` ⑦ mypy 0 errors on 51 source files |
| **W13** | ① `pytest tests/unit/test_agent_review.py tests/e2e/test_w13_dogfooding_e2e.py` 全过 ② `tools/agent_review.py` 拉取 PR diff（git diff main..HEAD）+ 7 类静态安全规则（secret_leak / cmd_injection / path_traversal / eval / sql_injection / data_exposure / weak_hash） ③ `${VAR}` SecretManager 引用被 `(?!\$\{)` negative lookahead 跳过 ④ 文件扩展名白名单（.py/.js/.ts/.go/.rs 等）+ 路径黑名单（.venv/node_modules/vendor/.git） ⑤ CLI: `--pr` / `--mode=simple|full` / `--output=text|json` 三件套 ⑥ 简单模式默认；完整模式（LLM + 对抗式）需 OPENAI_API_KEY / ANTHROPIC_API_KEY ⑦ G-001 Golden Case 跑通：能识别本项目 PR 中的真实安全问题 ⑧ `tools/verify_w13_dod.py` 5/5 通过 |

#### Phase 3 Weekly Slice DoD（W14-W21, 已落定）

> 详见 `docs/PHASE3-PLAN-2026-06-20.md` v2 修订表；W14 拆 a/b,W19 Docker 默认保守化,W20 MCP source 分级,每周 DoD 在 plan 内逐条列出。P3 整体通过 = W14a/W14b/W15/W16/W17/W18/W19/W20/W21 全部 §17.3 标记 P3 的 Golden Case 通过 + §17.4 CI 全绿 + `tools/verify_p3_dod.py` exit 0。

#### Phase 4 Weekly Slice DoD（W22-W27, 已落定）

| Week | 自动化 DoD |
|------|------|
| **W22** | ① `WorktreeManager(repo_root, base_dir)` + `WorktreeHandle` 导入可用 ② `acquire/release/list_active/get/cleanup_orphans/cleanup_all` 6 个 API 走通 ③ per-tenant `threading.Lock` 幂等,同 `(tenant, session, agent)` 重复 acquire 返回同一 handle ④ `_sanitize` 路径安全 + `_is_git_repo` 跨平台 (Windows 正反斜杠归一) ⑤ `tests/unit/test_worktree_manager.py` ≥26 cases 全过 ⑥ ruff 0 + mypy 0 |
| **W23** | ① `${WORKTREE_PATH}` 占位符注入到 `MCPServerConfig.command/cwd/env` ② `substitute_placeholders/validate_config/find_placeholders` 导出 ③ `WorktreeIntegration(manager)` 高层封装 ④ `examples/w22_mcp_worktree.yaml` 合法 + 2 worker 共享 repo ⑤ G-021 3/3 通过 (3 agent 100 文件 / 10 并发 / 跨租户) ⑥ `tools/bench_worktree.py` 压测脚本可跑 ⑦ `tools/verify_p4_dod.py` 8 项全过 |
| **W24** | ① `DockerConfig.long_lived: bool = True` 默认 ② `_start_container/_stop_container/_run_in_long_lived_container` 复用单容器 ③ 容器名 `agentswarm-<workspace_hash>-<pid>-<counter>` 唯一 ④ 100 次 `execute()` 只启 1 容器 (vs W19 模式 100 次) ⑤ `close()` + `__aenter__/__aexit__` async context manager 协议 ⑥ `long_lived=False` 保留 W19 兼容 ⑦ `tests/unit/test_sandbox_docker_long_lived.py` ≥13 cases ⑧ ruff 0 + mypy 0 |
| **W25** | ① `PostgresBackend` + `PostgresConfig` 协议匹配 `TaskQueueBackend` ② Schema `tasks(id PK, version INT, data JSONB, updated_at TIMESTAMPTZ)` ③ CAS 单语句原子 `UPDATE ... WHERE id=? AND version=? RETURNING data` ④ asyncpg 连接池 min=1/max=20 ⑤ `fake_module` 测试支持 ⑥ `tests/unit/test_task_queue_backends.py` PostgresBackend 段 ≥13 cases ⑦ ruff 0 + mypy 0 |
| **W26** | ① `DBCredentials` dataclass + `expires_at/seconds_to_expiry/is_expired/as_dsn` 派生属性 ② `VaultDynamicSecretManager` 继承 `VaultSecretManager` ③ `get_dynamic_credentials/renew_lease/revoke_lease/revoke_all/list_active_leases` 5 API ④ 端到端: get → 用 → revoke / 长期任务 renew ⑤ `tests/unit/test_vault_dynamic_secrets.py` ≥14 cases ⑥ ruff 0 + mypy 0 |
| **W27** | ① 0.4.0a1 sdist + wheel 构建成功 ② `twine check` PASSED ③ CHANGELOG 含 0.4.0a1 节点 ④ git tag 0.4.0a1..0.4.0a4 (4 个) ⑤ 全量 ≥1060 passed / 138 P3-WIN skipped / 0 failed ⑥ ruff 0 + mypy 0 ⑦ `tools/verify_p4_dod.py` exit 0 |

#### Phase 5 Weekly Slice DoD（W28-W32, GUI Web UI, 进行中）

> 计划详见 `docs/PHASE5-PLAN.md`；W29/W30 合并到 W31（W28 收尾后直接进 W31 CLI 集成,中间不另开切片——见 PHASE5-PLAN §0 拆分说明）。W33+ 候选待 P5 §17.3 收齐后回填。

| Week | 自动化 DoD |
|------|------|
| **W28** | ① `pip install -e ".[web]"` 后 `from agent_swarm.web import app` 可导入（fastapi/uvicorn/jinja2 落到 `[web]` extras） ② `create_app()` 工厂 + `lifespan` 上下文 + 4 页面 (`/` `/agents` `/worktrees` `/tasks`) + 5 partials + 3 JSON API + `/healthz` + `/metrics` (Prometheus 格式) + `WS /ws` 全部 200/101 ③ `WebState` 事件缓冲 500 条上限 + 多订阅者 fan-out + 断开清理 ④ 12 个 Jinja2 模板 (base + 4 pages + 5 partials + dashboard) + `style.css` 暗色主题 + `app.js` WebSocket 重连 ⑤ `examples/w28_web_demo.yaml` 2 worker 启动 ⑥ `tests/unit/test_web.py` ≥29 cases ⑦ ruff 0 + mypy 0 |
| **W29-W30** | **合并入 W31**（W28 收尾后未单开切片；W29 原计划"WebState 后端持久化"、W30 原计划"RBAC/auth v0"均下沉到 W33+ 候选,不在 P5 必交付范围） |
| **W31** | ① `WebStateSink(ObservabilitySink)` 接入 `ObservabilityBus`, consume SessionEvent 推入 `WebState` ② 异常内部吞掉 (warning log) + `drop_unsupported=False` 全推 + 不影响其他 sink ③ `tests/unit/test_web_state_sink.py` ≥10 cases ④ CLI `run` 命令新增 `--web/--web-host/--web-port` 三选项, 默认 host=127.0.0.1 port=8000 ⑤ `--web` 启用时: `WebState` + `WebStateSink` 注册 + 同进程 uvicorn 拉起 ⑥ import 失败 (未装 `[web]`) → 友好提示 + `sys.exit(2)` ⑦ `try/finally` 块 `web_server.should_exit=True` + 等待 `web_task` 干净关闭 ⑧ `examples/w31_web_with_swarm.yaml` 启动 ⑨ `agent-swarm run --help` 显示 3 个新选项 ⑩ ruff 0 + mypy 0 |
| **W32** | ① `create_app(worktree_manager: Any = None)` 关键字注入 ② `app.state.worktree_manager` 注册 (路由用 `getattr` 兜底) ③ CLI `run` 新增 `--web-worktree-repo PATH` (必须存在, git 仓库) + `--web-worktree-base PATH` (默认 `<repo>/.worktrees`) ④ enable_web + 提供 repo 时: `WorktreeManager(repo, base)` 实例化注入 `create_app` ⑤ `examples/w32_web_with_worktree.yaml` writer-A + writer-B 2 worker 启动 ⑥ `tests/unit/test_web.py` 增 ≥4 cases (create_app 接受 / 默认无 / partial_worktrees 真数据 / 空 manager 兜底) ⑦ ruff 0 + mypy 0 ⑧ 端到端: `WorktreeManager initialized` + `web UI started` |

#### Phase 级别 DoD

| Phase | DoD 清单（全部满足才算完成） |
|------|------|
| **Phase 1** | ① 6 个 Weekly Slice 全部 DoD 通过 ② 测试覆盖率 ≥75%（§17.4）③ §17.3 标记 P1 的 Golden Case 100% 通过 ④ §17.5 性能基线建立 ⑤ Quickstart 文档 + 3 个 examples 可独立运行 |
| **Phase 2** | ① 飞书连接器签名验证 + 卡片交互在真实 Lark 工作区可用 ② AdversarialVerifier 在 §17.3 的 5 个 P2 调试 case 上根因命中率 ≥80% ③ MCP 至少接入 2 个真实 server（GitHub + filesystem）④ Dogfooding 启动：≥10 个本项目 PR 经过 swarm 审 |
| **Phase 3** | ① 多租户压测 100 并发请求跨租户 0 越权 ② Redis 后端通过全部 Phase 1 测试 ③ Prometheus + Grafana 看板模板交付 ④ Docker sandbox 通过 CIS Docker Benchmark 关键项 |
| **Phase 4** | ① WorktreeManager 26+ tests + G-021 3/3 通过 ② Docker long_lived 100 次 execute 只启 1 容器 (vs W19 100 次) ③ PostgresBackend CRUD/CAS 全过 ④ VaultDynamicSecretManager get/renew/revoke 5 API 走通 ⑤ 0.4.0a1 sdist + wheel + twine check 全过 ⑥ 4 git tag (0.4.0a1-a4) ⑦ ruff 0 + mypy 0 + ≥1060 tests passed |
| **Phase 5** | ① Web UI v1 完整可启: `pip install -e ".[web]" && agent-swarm run examples/w31_web_with_swarm.yaml --web` ② WorktreeManager 闭环注入 Web UI (W22 预留 hook 落地) ③ 全量测试 0 failed (含 P3-WIN skipped ≤138) ④ 0.5.0a1 sdist + wheel + twine check 全过 ⑤ ruff 0 + mypy 0 ⑥ `tools/verify_p5_dod.py` exit 0 |

#### "完成"的反指标（出现即视为未完成）

- 测试是 `@pytest.mark.skip` 或 `assert True`
- LLM 调用全部 mock（无 e2e 用真实 LLM 验证）
- 异常路径无测试（只有 happy path）
- 文档示例无法复制粘贴跑通

### 17.3 验收场景库（Golden Cases）

> 维护 ~20 个真实案例 + 期望结果。既是 e2e 测试，也是性能/质量基线。
> 文件位置：`tests/golden/cases/<id>_<slug>/`，每个 case 是独立目录，含 `input.yaml` / `expected.yaml` / `README.md`。

#### Golden Case 结构

```yaml
# tests/golden/cases/G-001_pr_security_review/expected.yaml
id: G-001
title: PR 安全审查 - SQL 注入检出
phase: 1                              # 在哪个 Phase 必须通过
swarm_config: input.yaml              # swarm 输入
inputs:
  pr_diff: input_pr.diff              # 输入材料

expected:
  must_find:                          # 这些发现必须命中（顺序无关）
    - keyword: "SQL injection"
      location: "src/auth.py:42"
    - keyword: "missing parameterized query"
  must_not_claim:                     # 这些不能错报
    - keyword: "XSS"                  # 该 PR 不含 XSS
  performance:
    max_duration_seconds: 120
    max_total_tokens: 200_000
    max_cost_usd: 1.00
  quality:
    min_confidence: 0.7
```

#### MVP Golden Case 清单（20 个）

| ID | Phase | 类别 | 场景 | 期望验证 |
|----|-------|------|------|---------|
| G-001 | 1 | Code Review | PR 含 SQL 注入 | 必报、定位准确 |
| G-002 | 1 | Code Review | PR 含 N+1 查询 | 必报 |
| G-003 | 1 | Code Review | PR 干净（无问题） | 无误报 |
| G-004 | 1 | Recovery | 中断恢复（kill -9） | 状态完整重建 |
| G-005 | 1 | Recovery | 中断恢复（任务进行中） | 已完成不重做、未完成续跑 |
| G-006 | 1 | Security | 恶意 prompt 读 `/etc/passwd` | 被拦截 + emit security 事件 |
| G-007 | 1 | Security | 越权写 `~/.ssh/authorized_keys` | 被拦截 |
| G-008 | 1 | Performance | 大文件 (10MB) 读取 | 自动截断不崩溃、token 不爆 |
| G-009 | 1 | Concurrency | 5 agent 并发抢任务 | CAS 冲突可观测、最终一致 |
| G-010 | 1 | Provider Failover | OpenAI 限流 | 降级到 Anthropic 成功 |
| G-011 | 2 | Adversarial | 5 假设 5 专家调试生产 500 错误 | 根因为 DB 连接池耗尽，置信度 ≥0.7 |
| G-012 | 2 | Adversarial | 假设全错（所有假设都该被淘汰） | 返回 `all_eliminated`，不强行选 |
| G-013 | 2 | Adversarial | 不收敛（max_rounds 达到） | 多假设有序返回，rounds_used=5 |
| G-014 | 2 | Delegate | Lead 派发 3 任务给 worker | Lead 不执行任何工具调用、worker 各完成 1 |
| G-015 | 2 | Lark | @机器人 触发审查、卡片交互 | 端到端在测试 workspace 跑通 |
| G-016 | 2 | Approval | 高风险命令需要审批 | 用户拒绝 → agent 终止；超时 → 默认拒绝 |
| G-017 | 2 | MCP | GitHub MCP 创建 issue | 成功 + 触发 HIGH 审批 |
| G-018 | 2 | MCP | MCP server 崩溃 | 自动重连 3 次后熔断；agent 收到 ToolUnavailableError |
| G-019 | 3 | Multi-tenant | 100 并发跨租户读写 | 0 数据越权 |
| G-020 | 3 | Scale | 10 agent + Redis 后端 | 通过全部 Phase 1 case |

> Golden Case 列表本身要进版本控制；新加 case 必须 PR 评审，避免 case 漂移。

#### 运行方式

```bash
# CI 中
pytest tests/golden/ -m "phase==1"      # Phase 1 case 全跑
pytest tests/golden/ -m "phase<=2 and not slow"   # PR 守门只跑快速 case

# 本地排查单个 case
agent-swarm golden run G-011 --verbose
agent-swarm golden replay G-011         # 从录制的事件流回放，不调 LLM
```

#### Golden Case 与 LLM 不确定性

LLM 输出非确定性会让"必须命中关键词"变脆弱。处理策略：
1. **关键词检查 + 模糊匹配**：`must_find.keyword` 用大小写不敏感子串匹配，避免精确词序列依赖
2. **多次运行取通过率**：每个 case 跑 3 次，≥2 次通过算通过；0/3 视为失败
3. **置信度阈值而非"等于"**：用 `min_confidence` 区间判定，不要求精确浮点
4. **录制+回放兜底**：每次成功运行的事件流落盘，CI 关键路径用回放（不调真实 LLM），只在每日 nightly 跑真实 LLM

### 17.4 测试金字塔与 CI 门禁

#### 测试分层

```
            /\
           /  \           e2e (5%)：真实 LLM，调用真实 provider
          /----\          →  Golden Cases，nightly 运行
         /      \
        /--------\        integration (25%)：mock LLM，真实其他组件
       /          \       →  跨模块协作、CAS、Session 恢复
      /------------\
     /              \     unit (70%)：纯单元，零 IO
    /----------------\    →  单类、单函数、纯逻辑
```

| 层级 | 占比 | LLM 策略 | 速度目标 | 何时跑 |
|------|------|---------|---------|--------|
| unit | 70% | 不调 | 全套 ≤30 秒 | 每次 commit |
| integration | 25% | mock（固定回放）| 全套 ≤3 分钟 | PR 必跑 |
| e2e (Golden) | 5% | 真实 LLM | 全套 ≤30 分钟 | nightly + 手动 |

#### LLM Mock 策略

```python
# tests/conftest.py
@pytest.fixture
def mock_llm(monkeypatch):
    """
    LLM mock 策略——3 层降级

    1. 录制模式（pytest --llm-record）：跑真实 LLM 并把 (request, response) 存到
       tests/_recordings/<test_id>.jsonl，用于回放
    2. 回放模式（默认）：从录制文件读取，零网络调用
    3. 严格 mock：测试自己提供 stub_responses，验证特定边界（错误、超长输出等）
    """
```

录制文件随代码一起进版本控制（如同 VCR cassette），保证 CI 的可重复性。

#### CI Pipeline（GitHub Actions 示例）

```yaml
# .github/workflows/ci.yml（示意）
on: [pull_request]
jobs:
  fast-checks:                          # < 5 分钟，PR 阻塞
    steps:
      - ruff check                      # lint
      - mypy src/                       # 类型
      - pytest tests/unit/ --cov=src --cov-fail-under=75
      - pytest tests/integration/ -m "not slow"
      - python tools/check_sql_lint.py  # 检查所有 SQL 都注入了 WHERE tenant_id

  golden-replay:                        # < 10 分钟，PR 阻塞
    steps:
      - pytest tests/golden/ -m "phase==1" --llm-replay

  nightly-real-llm:                     # nightly 触发，不阻塞 PR
    schedule: "0 2 * * *"
    steps:
      - pytest tests/golden/ --llm-real
      - python tools/benchmark.py compare-baseline
```

#### CI 阻塞 PR 的硬规则

| 检查 | 阈值 | 失败处理 |
|------|------|---------|
| 单元测试覆盖率 | ≥75%（Phase 1）/ ≥80%（Phase 2）/ ≥85%（Phase 3）| 阻塞合并 |
| ruff + mypy | 0 error | 阻塞 |
| SQL lint（强制 WHERE tenant_id） | 0 violation | 阻塞 |
| Golden replay (Phase ≤ 当前) | 100% | 阻塞 |
| 性能基线劣化 | <20% | 警告（不阻塞）；>20% 阻塞 |
| 文档生成 | mkdocs build 无 warning | 阻塞 |

#### 测试要求的反指标（CI 应主动检测并失败）

```python
# tools/lint_tests.py
# 自动扫描并失败 PR 的反模式：
#   1. @pytest.mark.skip 没有 reason
#   2. assert True / pass-only test body
#   3. e2e 测试未标记 @pytest.mark.golden
#   4. integration 测试调用真实网络（用 pytest-socket 拦截）
```

### 17.5 性能与质量基线

> 把 §9.3 的"估算值"升级为"持续测量的基线"——每次 nightly 跑，劣化超过阈值报警。

#### Benchmark 工具

```python
# tools/benchmark.py
class Benchmark:
    """
    周度性能/质量基线

    对每个 Phase 的 Golden Case，记录：
    - duration_seconds（端到端）
    - total_tokens（prompt + completion）
    - cost_usd
    - quality_score（命中关键词数 / must_find 总数）
    """
    def run_all(self) -> BenchmarkReport: ...
    def compare_baseline(self, threshold_pct: float = 20.0) -> list[Regression]: ...
    def update_baseline(self) -> None:
        """人工确认后才更新基线（防 LLM 随机波动污染）"""
```

#### 基线文件

```yaml
# tests/golden/baseline.yaml（人工维护）
G-001_pr_security_review:
  duration_seconds: { p50: 45, p95: 80 }
  total_tokens: { p50: 120_000, p95: 180_000 }
  cost_usd: { p50: 0.35, p95: 0.55 }
  quality_score: { min: 0.85 }       # 至少 85% must_find 命中
  last_updated: 2026-06-16
  last_updated_by: "@username"
```

#### 报警规则

| 维度 | 警告阈值 | 阻塞阈值 | 处理 |
|------|---------|---------|------|
| duration p95 | +20% | +50% | 警告 / 阻塞合并 |
| total_tokens p95 | +20% | +40% | 同上 |
| cost_usd p95 | +20% | +40% | 同上 |
| quality_score min | -5% | -15% | 同上（质量劣化最严重） |

劣化原因清单（排查参考）：
- prompt 模板被改长 → 检查 git blame system prompt
- 新增工具 schema 增加 token → 评估必要性
- LLM provider 改了模型 → 锁定 model 版本
- KB 缓存失效 → 检查 cache_analysis 调用点

### 17.6 风险登记表

| 风险 | 概率 | 影响 | 早期信号 | 应对 |
|------|------|------|---------|------|
| MCP 协议 spec 变更 | 中 | 中 | Anthropic blog / changelog | 锁定 SDK 版本，每月 review；适配层隔离 |
| 飞书 API 改版 | 中 | 中 | Lark 开发者通知 | LarkConnector 单独可替换；卡片模板版本化 |
| LLM provider 限流突变 | 高 | 高 | nightly 失败率上升 | 每个 provider 独立 circuit breaker；多 provider failover（G-010） |
| AdversarialVerifier 不收敛 | 中 | 高 | rounds_used 经常 ≥4 | max_rounds 强制截断；Golden Case G-013 监控 |
| SQLite WAL 在容器/NFS 不可靠 | 高 | 中 | flock 警告日志 | §10.2 已警告；生产强制 Redis |
| Agent 死循环烧 token | 中 | 高 | tokens_used 突增 | TokenBudget 硬上限 + max_tokens_per_task；超限自动 stop |
| Prompt injection 越权 | 高 | 高 | security.policy_check denied 事件 | SecurityPolicy 黑白名单 + Sandbox + Approval；G-006/G-007 持续验证 |
| LLM 输出 schema 漂移 | 中 | 中 | structured output 解析失败率 | Provider 层 retry + JSON Schema 强制约束；fallback 到自由文本 + 解析 |
| 测试 LLM 真实调用成本失控 | 中 | 中 | nightly cost > $X | Golden Case 用 cheap model（gpt-4o-mini）跑大部分；旗舰模型只跑 P1 case |
| TUI 在 Windows 终端兼容性 | 高 | 低 | Issue 反馈 | Textual 跨平台测试；fallback 到非 TUI mode |

> 风险表每月 review 一次，新增/淘汰由 PR 评审决定。

### 17.7 Dogfooding 与开发者体验（DX）

#### Dogfooding 计划

| 阶段 | 时机 | 内容 |
|------|------|------|
| 准备 | Phase 1 W6 | 在 `examples/dogfood_pr_review.yaml` 配置自审 swarm |
| 启动 | Phase 2 末期 | 强制：本项目所有 PR 必须先经过 swarm review，结果作为评论发到 PR |
| 反馈循环 | Phase 2-3 | 每周收集 dogfooding 中的 false positive / false negative，回写到 Golden Case |
| 公开演示 | Phase 3 | 把"agent-swarm 用 agent-swarm 自审 agent-swarm 的 PR" 作为案例发布 |

#### 开发者体验工具（DX Tools）

| 工具 | 命令 | 用途 |
|------|------|------|
| 会话回放 | `agent-swarm session replay <id>` | 不调 LLM，按事件流时间线回放 swarm 行为，断点调试 |
| 单 agent 调试 | `agent-swarm debug-agent <agent-id>` | 单步执行 observe→think→act→reflect，每步可注入 prompt 修正 |
| Token 火焰图 | `agent-swarm session profile <id>` | 显示哪个 agent / 哪轮 / 哪个工具调用消耗了最多 token |
| Prompt diff | `agent-swarm prompt diff <agent-id> <round-a> <round-b>` | 对比同一 agent 不同轮次的 prompt 变化（排查 KB/CC 变化导致的行为偏移） |
| Golden 录制 | `agent-swarm golden record <case-id>` | 把当前真实运行结果录制为 baseline.yaml 候选，人工确认后入库 |
| 健康检查 | `agent-swarm doctor` | 检查 LLM provider 连通性、SQLite 锁、MCP server 状态、密钥就位 |

#### 文档交付清单（Phase 1 末期必备）

- `README.md`：5 分钟 quickstart
- `docs/concepts.md`：核心概念图解
- `docs/recipes/`：≥5 个常见任务（PR 审查 / 调试 / 文档生成 / ...）
- `docs/troubleshooting.md`：常见错误 + Golden Case 链接
- `docs/api/`：mkdocs 自动生成
- 每个 example 自带 README + 预期输出

### 17.8 交付门禁总览（一图看完）

```
每次 commit:
  ├─ pre-commit: ruff / mypy / SQL lint            (本地, < 5s)
  └─ unit tests                                    (本地, < 30s)

每个 PR:
  ├─ fast-checks: lint + unit + integration         (CI, < 5min)
  ├─ golden-replay: Phase ≤ current 全过            (CI, < 10min)
  ├─ coverage gate: 75% / 80% / 85%                (CI, 阻塞)
  └─ docs build: mkdocs 无 warning                 (CI, 阻塞)

每天 nightly:
  ├─ golden-real-llm: 真实 LLM 跑全部 Golden Case   (CI, < 30min)
  ├─ benchmark: 性能/质量基线对比                    (CI, 报警)
  └─ security scan: prompt injection 攻击套件        (CI, 报警)

每周里程碑:
  ├─ Weekly Slice DoD 验证                          (人工 + CI)
  ├─ git tag w<N>-demo + 录屏                       (人工)
  └─ 风险登记表 review                              (人工)

每个 Phase 结束:
  ├─ Phase DoD 全过                                 (机器验证)
  ├─ Dogfooding 反馈回写                            (人工)
  └─ baseline.yaml 更新                             (人工签字)
```

---

## 附录 A：核心数据类型字典

> v4.1 新增——前文出现但未定义的 dataclass 集中收口，避免读者翻找。
> 此处按需补完，作为契约规格；具体实现以本附录为准。

### A.1 编排与任务

```python
@dataclass
class SwarmStatus:
    """Swarm 实时状态——swarm.status() 返回值"""
    name: str
    session_id: str
    state: Literal["idle", "running", "paused", "stopped", "completed"]
    started_at: float
    uptime_seconds: float
    agents: list[AgentStatusSnapshot]
    tasks_summary: dict[str, int]      # {"pending": 3, "in_progress": 1, ...}
    metrics: FrameworkMetrics

@dataclass
class AgentStatusSnapshot:
    agent_id: str
    role: str
    state: Literal["idle", "thinking", "acting", "reflecting", "waiting"]
    current_task_id: str | None
    tokens_used: int

@dataclass
class SwarmResult:
    """Swarm.run() 返回值——本次运行的最终结果"""
    session_id: str
    state: Literal["completed", "stopped", "failed"]
    duration_seconds: float
    tasks_completed: int
    tasks_failed: int
    final_outputs: dict[str, Any]      # task_id → result
    error: str | None = None

@dataclass
class ProtocolResult:
    """CollaborationProtocol.execute() 返回值"""
    success: bool
    output: Any                        # 协议特定的结果（如 Verdict）
    rounds_used: int = 0
    error: str | None = None

@dataclass
class SessionSummary:
    """SessionManager.list_sessions() 列表项"""
    session_id: str
    swarm_name: str
    state: str
    started_at: float
    ended_at: float | None
    tenant_id: str
```

### A.2 LLM 与工具

```python
@dataclass
class LLMResponse:
    """LLMProvider.chat() 返回值"""
    content: str
    tool_calls: list[ToolCall]
    finish_reason: Literal["stop", "tool_use", "length", "content_filter"]
    tokens_prompt: int
    tokens_completion: int
    model: str
    latency_ms: float

@dataclass
class LLMChunk:
    """流式响应片段"""
    delta_text: str | None = None
    delta_tool_call: ToolCall | None = None
    finish_reason: str | None = None

@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]

@dataclass
class TokenBudget:
    """TokenBudgetManager.calculate_usage() 返回值"""
    used_tokens: int
    limit_tokens: int
    reserve_tokens: int
    remaining_tokens: int
    needs_truncation: bool

@dataclass
class PricingInfo:
    """LLMProvider.pricing 属性"""
    prompt_per_1k: float               # 美元
    completion_per_1k: float
    currency: str = "USD"

@dataclass
class ToolSchema:
    name: str
    description: str
    parameters: dict                   # JSON Schema
    risk: ToolRisk = ToolRisk.MEDIUM
```

### A.3 安全与审批

```python
@dataclass
class PolicyDecision:
    """SecurityPolicy.check_tool() 返回值"""
    decision: Literal["ALLOW", "DENY", "REQUIRE_APPROVAL"]
    reason: str
    auto_sandbox: bool = False         # 是否强制走 SandboxManager

@dataclass
class ApprovalRequest:
    """审批发起时的载荷"""
    approval_id: str
    agent_id: str
    tool_name: str
    arguments: dict[str, Any]
    risk: ToolRisk
    description: str                   # 给人看的简述
    related_task_id: str | None = None
    related_task_version: int | None = None  # 用于审批通过后 CAS 校验

@dataclass
class ApprovalResult:
    approval_id: str
    decision: Literal["approved", "rejected", "timeout"]
    decided_by: ChannelUser | None     # 谁批的（None=超时）
    decided_at: float
    comment: str = ""

@dataclass
class SandboxResult:
    """SandboxManager.execute() 返回值"""
    exit_code: int
    stdout: str
    stderr: str
    truncated: bool                    # 输出是否被 max_output_bytes 截断
    duration_seconds: float
    timed_out: bool = False
```

### A.4 知识与对话

```python
@dataclass
class Document:
    """KnowledgeBase 中的项目文档"""
    path: str
    content: str
    last_modified: float
    metadata: dict[str, Any] = field(default_factory=dict)

@dataclass
class CodeSnippet:
    """KnowledgeBase.search_code() 返回项"""
    file_path: str
    line_start: int
    line_end: int
    content: str
    language: str
    score: float = 0.0                 # 相关性分数

# Turn 与 ExternalInput 已在 §6.6 定义
```

### A.5 通道与会话

```python
@dataclass
class SessionEvent:
    """已在 §6.7 定义；事件名规范见 §5.4"""
    event_name: str
    session_id: str
    timestamp: float
    payload: dict[str, Any]
    request_id: str | None = None

# ChannelMessage / ChannelResponse / ChannelUser 见 §4.2
# SecurityContext 见 §8.4
```

---

## 附录 B：v4 修订摘要

> v4.1 → v4.2 的修订见文首"v4.2 修订摘要"。
> 此处归档 v3 → v4 → v4.1 的修订。

### B.1 v4.0 → v4.1（一致性修复）

| # | 问题 | 修复 |
|---|------|------|
| 1 | §15 Phase 1 未列 SecurityContext（与 §8.4 矛盾） | Phase 1 显式加入 SecurityContext 基建 |
| 2 | §1 核心理念表残留 v3 用语（"Lock"） | 改为"乐观锁 CAS"；Lead 表述统一为"只编排，不动手" |
| 3 | §2 架构图 Storage 区还显示 In-Memory | 改为 SQLite (file/`:memory:`) / Redis / File |
| 4 | §6.1 `class DelegateMode(Protocol)` 与 typing.Protocol 撞名 | 协议基类改名 `CollaborationProtocol` |
| 5 | §6.7 `SessionEventType` enum 与 §5.4 点分式命名冲突 | 删除 enum，统一为字符串事件名 |
| 6 | §8.4 `scope()` 缺少 `@contextmanager` | 补全装饰器，区分同步/异步 scope |
| 7 | §6.2 / §6.6 关于 ConversationContext 的隔离矛盾 | 引入 `external_inputs` 字段 |
| 8 | §13 `security/tenant.py` 已过时 | 改名为 `security_context.py` |
| 9 | 大量数据类型未定义 | 新增附录 A 核心数据类型字典 |
| 10 | `claim() -> None` 三种语义合一 | 引入 `ClaimResult` 携带失败原因 |
| 11 | `SecurityContext.capabilities_override` 半成品字段 | 删除该字段 |
| 12 | KnowledgeBase 跨租户泄露风险 | KB 改为 per-tenant 实例 |
| 13 | ApprovalFlow 回调如何获取 SecurityContext | 注册时自动捕获 ctx，触发时 async_scope 进入 |
| 14 | MCP 凭证/重连策略未提 | 凭证走 SecretManager；崩溃自动重连 + 熔断 |
| 15 | `acquire_lock` 留在 ABC 但 MVP 无用例 | MVP ABC 中删除（YAGNI） |

### B.2 v3 → v4（设计修复）

| # | 问题 | 修复 |
|---|------|------|
| 1 | AdversarialVerifier 算法不完整 | §6.2 补完一轮的完整流程、判定算法、终止条件、兜底 |
| 2 | Task Queue 悲观锁 + 乐观锁并存 | §6.4 统一为乐观锁 CAS（v4.1 进一步删除 acquire_lock） |
| 3 | Agent `mode` / `allowed_actions` / `risk_profile` 语义重叠 | §7.1 合并为 `AgentCapabilities` |
| 4 | KnowledgeBase 与 ConversationContext 拆分动机不充分 | §6.6 补充三条不可合并的具体场景 |
| 5 | ObservabilityBus 抽象但无落地调用 | §5.4 补事件命名目录、emit 点 |
| 6 | `tenant_id` 渗透到所有 API 签名 | §8.4 改用 `SecurityContext` (contextvars) |
| 7 | In-Memory 后端与 SQLite 重复 | §10.2 改为 SQLite `:memory:` 别名 |
| 8 | MCP 工具集成未表态 | §7.3 明确支持 |
