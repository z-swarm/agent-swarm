"""
@module agent_swarm.tui.app
@brief  SwarmDashboardApp —— Textual TUI 仪表盘（W6）

DESIGN.md §17.1 W6 DoD: TUI 启动后 5 秒内显示完整 swarm 视图

布局 (Grid 2x2):
  ┌─────────────────┬──────────────────┐
  │ Swarm Status    │ Task Queue       │
  │ (name/uptime/   │ (id/status/owner)│
  │  agent list)    │                  │
  ├─────────────────┼──────────────────┤
  │ Message Stream  │ Token Budget     │
  │ (滚动 from→to)  │ (粗估 used/limit)│
  └─────────────────┴──────────────────┘

设计:
  - 4 个 panel 都是反应式 data store（dataclass + watcher）
  - 后台 worker 协程从 TUISink.queue 持续拉事件 → 调 panel.update()
  - Event dispatch 表: 事件名前缀 → 哪个 panel
  - 进程结束 (swarm.completed / swarm.failed) 后 App 持续显示, 用户按 q 退出
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar

from textual.app import App, ComposeResult
from textual.containers import Grid
from textual.reactive import reactive
from textual.widgets import DataTable, Footer, Header, Static

from agent_swarm.core.types import SessionEvent
from agent_swarm.tui.sink import TUISink

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 面板数据模型（dataclass 持有渲染所需状态）
# ---------------------------------------------------------------------------


@dataclass
class AgentInfo:
    """@brief 单 agent 的运行时摘要"""

    agent_id: str
    model: str = "?"
    status: str = "idle"  # idle / running / done
    tasks_done: int = 0


@dataclass
class TaskRow:
    """@brief Task Queue 表格的一行"""

    task_id: str
    title: str
    status: str  # pending / in_progress / completed / failed / blocked
    owner: str = "-"


@dataclass
class MessageRow:
    """@brief Message Stream 的一行"""

    timestamp: str
    src: str
    dst: str
    preview: str


@dataclass
class SwarmStatusData:
    """@brief Swarm Status 面板的可观察状态"""

    name: str = "(starting...)"
    state: str = "pending"  # pending / running / completed / failed
    session_id: str = "-"
    started_at: float | None = None
    agents: dict[str, AgentInfo] = field(default_factory=dict)
    tasks_total: int = 0
    tasks_completed: int = 0
    tasks_failed: int = 0

    @property
    def uptime(self) -> str:
        """@brief 已运行时长, 状态未开始时返回 '-'"""
        if self.started_at is None:
            return "-"
        delta = datetime.now().timestamp() - self.started_at
        return f"{delta:.1f}s"


@dataclass
class TokenBudgetData:
    """@brief Token Budget 面板的运行时统计"""

    # 粗估: 1 token ≈ 4 chars（与 TokenBudgetManager 一致）
    CHARS_PER_TOKEN: ClassVar[int] = 4
    # 假设默认上下文上限 (W6 简化; W7 接入 per-model 真实限制)
    DEFAULT_LIMIT: ClassVar[int] = 128_000

    used_tokens: int = 0
    last_task_tokens: int = 0
    last_task_id: str = "-"

    def add_result(self, result: Any) -> int:
        """
        @brief 把 task.completed 事件的 result 折算成 tokens 并累加
        @return 本次新增的 token 数
        """
        text = result if isinstance(result, str) else str(result)
        added = max(1, len(text) // self.CHARS_PER_TOKEN)
        self.used_tokens += added
        return added


# ---------------------------------------------------------------------------
# 4 个面板 widget
# ---------------------------------------------------------------------------


class SwarmStatusPanel(Static):
    """@brief Swarm Status 面板——单 agent + 状态文本块"""

    data: SwarmStatusData

    def __init__(self, data: SwarmStatusData) -> None:
        super().__init__(id="panel-status")
        self.data = data

    def render_status(self) -> None:
        """@brief 把 data 重新渲染到 widget"""
        lines = [
            f"[b cyan]{self.data.name}[/b cyan]  [dim]state=[/dim][b]{self.data.state}[/b]",
            f"session: {self.data.session_id}",
            f"uptime:  {self.data.uptime}",
            f"tasks:   {self.data.tasks_completed}/{self.data.tasks_total} completed, "
            f"{self.data.tasks_failed} failed",
            "",
            "[b]Agents:[/b]",
        ]
        if not self.data.agents:
            lines.append("  (none)")
        for a in self.data.agents.values():
            lines.append(f"  - {a.agent_id}  [dim]({a.model})[/dim]  done={a.tasks_done}")
        self.update("\n".join(lines))


class TaskQueuePanel(Static):
    """@brief Task Queue 面板——DataTable 展示所有 task 的状态流转"""

    data: dict[str, TaskRow]
    _table: DataTable

    def __init__(self) -> None:
        super().__init__(id="panel-tasks")
        self.data = {}

    def compose(self) -> ComposeResult:
        self._table = DataTable(zebra_stripes=True)
        self._table.add_columns("task_id", "status", "owner", "title")
        yield self._table

    def upsert(self, row: TaskRow) -> None:
        """@brief 插入或更新一行"""
        self.data[row.task_id] = row
        self._refresh()

    def _refresh(self) -> None:
        self._table.clear()
        for r in self.data.values():
            self._table.add_row(r.task_id, r.status, r.owner, r.title[:48])


class MessageStreamPanel(Static):
    """@brief Message Stream 面板——最多保留最近 100 条消息"""

    MAX_ROWS: int = 100
    _table: DataTable
    data: list[MessageRow]

    def __init__(self) -> None:
        super().__init__(id="panel-messages")
        self.data = []

    def compose(self) -> ComposeResult:
        self._table = DataTable(zebra_stripes=True)
        self._table.add_columns("time", "from", "to", "preview")
        yield self._table

    def append(self, row: MessageRow) -> None:
        self.data.append(row)
        if len(self.data) > self.MAX_ROWS:
            self.data = self.data[-self.MAX_ROWS :]
        self._table.clear()
        for r in self.data[-50:]:  # 只渲染最近 50 行
            self._table.add_row(r.timestamp, r.src, r.dst, r.preview[:64])


class TokenBudgetPanel(Static):
    """@brief Token Budget 面板——粗估已用 + 上限 + 最近 task"""

    data: TokenBudgetData

    def __init__(self, data: TokenBudgetData) -> None:
        super().__init__(id="panel-budget")
        self.data = data

    def render_budget(self) -> None:
        used = self.data.used_tokens
        limit = self.data.DEFAULT_LIMIT
        pct = min(100.0, used * 100.0 / limit)
        bar_width = 30
        filled = int(pct / 100.0 * bar_width)
        bar = "█" * filled + "░" * (bar_width - filled)
        self.update(
            f"[b]Token Budget (粗估)[/b]\n"
            f"used:  {used:>7,} / {limit:,}  ({pct:.1f}%)\n"
            f"[cyan]{bar}[/cyan]\n"
            f"last task: {self.data.last_task_id}  +{self.data.last_task_tokens} tokens"
        )


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------


class SwarmDashboardApp(App):
    """
    @brief  W6 TUI 仪表盘 App

    @note 启动流程:
      1) 把 TUISink 注册到全局 ObservabilityBus
      2) 后台 _pump_events() 协程从 TUISink.queue 拉事件 → 路由到面板
      3) 1 秒定时 refresh() 重新渲染 Static 面板
      4) swarm.completed / swarm.failed 时设置 is_finished=True, App 仍可交互
    """

    CSS = """
    Grid {
        grid-size: 2 2;
        grid-gutter: 1;
        height: 100%;
    }
    SwarmStatusPanel, TaskQueuePanel, MessageStreamPanel, TokenBudgetPanel {
        border: round $primary;
        padding: 1;
    }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
    ]

    # 反应式全局状态
    swarm_status: reactive[SwarmStatusData] = reactive(SwarmStatusData(), recompose=False)
    token_data: reactive[TokenBudgetData] = reactive(TokenBudgetData(), recompose=False)

    def __init__(self, sink: TUISink, swarm_name: str = "?") -> None:
        super().__init__()
        self._sink = sink
        self._is_finished = False
        # 共享 data store
        self._status_data = SwarmStatusData(name=swarm_name)
        self._budget_data = TokenBudgetData()
        self._task_rows: dict[str, TaskRow] = {}
        self._msg_rows: list[MessageRow] = []

    # ------------------------------------------------------------------
    # 布局
    # ------------------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Grid():
            self._status_panel = SwarmStatusPanel(self._status_data)
            self._task_panel = TaskQueuePanel()
            self._msg_panel = MessageStreamPanel()
            self._budget_panel = TokenBudgetPanel(self._budget_data)
            yield self._status_panel
            yield self._task_panel
            yield self._msg_panel
            yield self._budget_panel
        yield Footer()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def on_mount(self) -> None:
        """@brief App 挂载时启动后台协程 (F-09: 显式 context 防 ctx 丢)"""
        # F-09: TUI 后台 task 显式传 ctx——TUI 启动时通常无 SecurityContext 包裹
        try:
            from agent_swarm.security.context import SecurityContextManager

            ctx = SecurityContextManager.current_or_default(session_id="tui")
        except Exception:
            ctx = None
        if ctx is not None:
            task_ctx = ctx.asyncio_context()
            self._pump_task = asyncio.create_task(self._pump_events(), context=task_ctx)
            self._refresh_task = asyncio.create_task(self._refresh_loop(), context=task_ctx)
        else:
            self._pump_task = asyncio.create_task(self._pump_events())
            self._refresh_task = asyncio.create_task(self._refresh_loop())

    async def on_unmount(self) -> None:
        """@brief App 卸载时取消后台协程"""
        for t in (self._pump_task, self._refresh_task):
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t

    # ------------------------------------------------------------------
    # 事件泵
    # ------------------------------------------------------------------
    async def _pump_events(self) -> None:
        """
        @brief 持续从 TUISink.queue 拉事件, 路由到对应面板

        @note 用 asyncio.wait_for 短超时, 让 cancelled 能被快速响应
        @note W43a: drain 模式 — 队列非空时一次拉多个事件 (max_drain=1000 兜底),
              减少 wait_for 0.5s 阻塞, 加速大场景 (100 task 涌入) 处理
        """
        max_drain = 1000
        while not self._is_finished or not self._sink.queue.empty():
            # W43a: drain 队列 (空时快速 fallthrough 到 wait_for)
            drained = 0
            while not self._sink.queue.empty() and drained < max_drain:
                try:
                    evt = self._sink.queue.get_nowait()
                except asyncio.QueueEmpty:  # noqa: PERF203
                    break
                self._dispatch(evt)
                drained += 1
            if drained == 0:
                # 队列空, 短 wait 等新事件
                try:
                    evt = await asyncio.wait_for(self._sink.queue.get(), timeout=0.5)
                except TimeoutError:
                    continue
                self._dispatch(evt)
        # 完成后 2 秒内自动退出（Q6 demo 用; 真实部署可改成持续监听）
        await asyncio.sleep(2.0)
        self.exit()

    def _dispatch(self, evt: SessionEvent) -> None:
        """@brief 把单条事件路由到对应面板的更新逻辑"""
        try:
            if evt.event_name == "swarm.started":
                self._on_swarm_started(evt)
            elif evt.event_name == "swarm.completed":
                self._on_swarm_done(evt, state="completed")
            elif evt.event_name == "swarm.failed":
                self._on_swarm_done(evt, state="failed")
            elif evt.event_name.startswith("task."):
                self._on_task_event(evt)
            elif evt.event_name.startswith("message."):
                self._on_message_event(evt)
        except Exception as exc:  # noqa: BLE001
            log.warning("tui.dispatch_error event=%s err=%s", evt.event_name, exc)

    # ---- swarm.* ----
    def _on_swarm_started(self, evt: SessionEvent) -> None:
        p = evt.payload
        self._status_data.name = p.get("name", self._status_data.name)
        self._status_data.session_id = evt.session_id
        self._status_data.state = "running"
        self._status_data.started_at = evt.timestamp
        self._status_data.tasks_total = p.get("task_count", 0)
        # P-3 修复:优先用 agents[] 拿 model/role(Phase 1 后 payload 含此字段);
        # 回退到 agent_ids 仅拿 id——保持向后兼容旧 event log 重放
        for ag in p.get("agents", []):
            aid = ag.get("id", "?")
            self._status_data.agents[aid] = AgentInfo(
                agent_id=aid,
                model=ag.get("model", "?"),
                status="idle",
            )
        for aid in p.get("agent_ids", []):
            if aid not in self._status_data.agents:
                self._status_data.agents[aid] = AgentInfo(agent_id=aid)

    def _on_swarm_done(self, evt: SessionEvent, state: str) -> None:
        self._status_data.state = state
        self._status_data.tasks_completed = evt.payload.get("tasks_completed", 0)
        self._status_data.tasks_failed = evt.payload.get("tasks_failed", 0)
        self._is_finished = True

    # ---- task.* ----
    def _on_task_event(self, evt: SessionEvent) -> None:
        p = evt.payload
        tid = p.get("task_id", "?")
        title = p.get("title", self._task_rows.get(tid, TaskRow(tid, "?", "pending")).title)
        if evt.event_name == "task.created":
            status = "pending"
            owner = "-"
        elif evt.event_name == "task.claimed":
            status = "in_progress"
            owner = p.get("agent_id", "-")
        elif evt.event_name == "task.completed":
            status = "completed"
            owner = self._task_rows.get(tid, TaskRow(tid, title, "pending")).owner
            added = self._budget_data.add_result(p.get("result", ""))
            self._budget_data.last_task_tokens = added
            self._budget_data.last_task_id = tid
        elif evt.event_name == "task.failed":
            status = "failed"
            owner = self._task_rows.get(tid, TaskRow(tid, title, "pending")).owner
        elif evt.event_name == "task.unblocked":
            status = "pending"  # 解阻塞后等待认领
            owner = "-"
        else:
            return
        self._task_panel.upsert(TaskRow(task_id=tid, title=title, status=status, owner=owner))

    # ---- message.* ----
    def _on_message_event(self, evt: SessionEvent) -> None:
        p = evt.payload
        ts = datetime.fromtimestamp(evt.timestamp).strftime("%H:%M:%S")
        self._msg_panel.append(
            MessageRow(
                timestamp=ts,
                src=p.get("from", "?"),
                dst=p.get("to", "?"),
                preview=str(p.get("subject") or p.get("preview") or "")[:80],
            )
        )

    # ------------------------------------------------------------------
    # 定时刷新（Static 面板需要主动重渲染）
    # ------------------------------------------------------------------
    async def _refresh_loop(self) -> None:
        """@brief 每秒刷新 Static 面板（uptime 等动态字段）"""
        while True:
            self._status_panel.render_status()
            self._budget_panel.render_budget()
            await asyncio.sleep(1.0)


# ---------------------------------------------------------------------------
# CLI / 外部调用入口
# ---------------------------------------------------------------------------


async def run_dashboard(
    swarm: Any,
    sink: TUISink | None = None,
) -> None:
    """
    @brief 在 TUI 中运行 swarm, 自动注册 TUISink

    @param swarm  Swarm 实例（必须已 set_global_bus + ObservableBus.register_sink）
    @param sink   可选注入——为 None 时新建一个 TUISink 并自动注册到全局 bus
    """
    from agent_swarm.observability import get_global_bus  # 避免循环导入

    bus = get_global_bus()
    if bus is None:
        raise RuntimeError("run_dashboard requires set_global_bus() to be called first")

    own_sink = sink is None
    if sink is None:
        sink = TUISink()
    bus.register_sink(sink)

    app = SwarmDashboardApp(sink, swarm_name=swarm.name)
    try:
        # F-09: 显式传 ctx 给 swarm.run() 任务
        try:
            from agent_swarm.security.context import SecurityContextManager

            ctx = SecurityContextManager.current_or_default(session_id=swarm.session_id)
            task_ctx = ctx.asyncio_context()
            swarm_task = asyncio.create_task(swarm.run(), context=task_ctx)
        except Exception:
            swarm_task = asyncio.create_task(swarm.run())
        await app.run_async()
        # TUI 退出后, 等 swarm 也跑完（如果还活着）
        if not swarm_task.done():
            swarm_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await swarm_task
    finally:
        if own_sink:
            with contextlib.suppress(Exception):
                await sink.aclose()
