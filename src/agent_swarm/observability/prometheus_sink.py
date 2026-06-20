"""
@module agent_swarm.observability.prometheus_sink
@brief  W15 PrometheusSink——DESIGN §17.5 / §15.3 Prometheus 导出

W15 范围：
  - 5 个核心指标（DESIGN §15.3 + P3-PLAN-v2 W15 DoD ③）：
    1. framework_tasks_total: Counter{task_status} 任务状态计数
    2. framework_llm_tokens_total: Counter{provider, model, kind}  LLM token 消耗
    3. framework_cas_conflict_total: Counter{entity}  CAS 冲突（version_mismatch）
    4. framework_mcp_circuit_state: Gauge{server}  MCP 熔断状态（0=closed, 1=open）
    5. framework_approval_pending_count: Gauge  待审批请求数
  - 暴露 GET /metrics 端点：aiohttp 集成（DESIGN §10.1 已有 aiohttp 依赖）
  - 可选：prometheus_client.start_http_server（默认关闭；用 aiohttp 集成更可控）

设计要点：
  - consume() 从 event.payload 提取指标数据；不感知具体业务逻辑
  - 业务模块 emit 特定事件：task.completed / llm.call_completed / cas.conflict / mcp.circuit_changed / approval.requested 等
  - 默认指标标签保守（避免高基数）：只加 model 名等有限维度
  - /metrics 端点单独 aiohttp app：可挂到主服务端口的子路径
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiohttp import web

from agent_swarm.core.types import SessionEvent
from agent_swarm.observability.bus import ObservabilitySink

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 5 个核心指标定义（DESIGN §15.3）
# ---------------------------------------------------------------------------

# 延迟 import：prometheus_client 是可选依赖，W15 强制装上
try:
    from prometheus_client import (
        CONTENT_TYPE_LATEST,
        CollectorRegistry,
        Counter,
        Gauge,
        generate_latest,
    )
    _PROMETHEUS_AVAILABLE = True
except ImportError:
    _PROMETHEUS_AVAILABLE = False
    # 这些名字在 ImportError 分支下不应被访问；提供 fallback 让 mypy 闭嘴
    CollectorRegistry = None  # type: ignore[assignment,misc]
    Counter = None  # type: ignore[assignment,misc]
    Gauge = None  # type: ignore[assignment,misc]
    generate_latest = None  # type: ignore[assignment]
    CONTENT_TYPE_LATEST = "text/plain; version=0.0.4; charset=utf-8"


def _require_prometheus() -> None:
    if not _PROMETHEUS_AVAILABLE:
        raise ImportError(
            "prometheus_client is required for PrometheusSink; "
            "install with: pip install prometheus-client"
        )


# 任务状态计数器
TASKS_TOTAL = "framework_tasks_total"
# LLM token 消耗
LLM_TOKENS_TOTAL = "framework_llm_tokens_total"
# CAS 冲突次数
CAS_CONFLICT_TOTAL = "framework_cas_conflict_total"
# MCP 熔断状态（0=closed, 1=open）
MCP_CIRCUIT_STATE = "framework_mcp_circuit_state"
# 待审批请求数
APPROVAL_PENDING_COUNT = "framework_approval_pending_count"


# 任务状态枚举（counter label 取值）
TASK_STATUS_LABELS = (
    "pending",
    "blocked",
    "in_progress",
    "completed",
    "failed",
)


class PrometheusSink(ObservabilitySink):
    """
    Prometheus metrics sink + /metrics HTTP 端点

    5 个核心指标（DESIGN §15.3）:
      - framework_tasks_total{task_status}
      - framework_llm_tokens_total{provider, model, kind}  (kind=prompt|completion)
      - framework_cas_conflict_total{entity}  (entity=task|message|kb)
      - framework_mcp_circuit_state{server}  (0=closed, 1=open)
      - framework_approval_pending_count  (gauge, no labels)

    @note 业务模块 emit 约定事件:
      - task.created / task.claimed / task.completed / task.failed:
        → framework_tasks_total{task_status=...}.inc()
      - llm.call_completed:
        → framework_llm_tokens_total{provider, model, kind=prompt}.inc(prompt_tokens)
        → framework_llm_tokens_total{provider, model, kind=completion}.inc(completion_tokens)
      - cas.conflict (payload 含 entity + version):
        → framework_cas_conflict_total{entity=...}.inc()
      - mcp.circuit_changed (payload 含 server + state):
        → framework_mcp_circuit_state{server=...}.set(0 if state=="closed" else 1)
      - approval.requested / approval.resolved:
        → framework_approval_pending_count.inc() / .dec()

    @note HTTP 端点默认关闭；用 start_http_server(host, port) 显式启动
    """

    def __init__(self, registry: Any | None = None) -> None:
        _require_prometheus()
        # 用独立 registry 避免污染全局（也方便测试）
        self.registry = registry or CollectorRegistry()
        self._tasks_total = Counter(
            TASKS_TOTAL,
            "Total task state transitions",
            ["task_status"],
            registry=self.registry,
        )
        self._llm_tokens_total = Counter(
            LLM_TOKENS_TOTAL,
            "Total LLM tokens consumed",
            ["provider", "model", "kind"],
            registry=self.registry,
        )
        self._cas_conflict_total = Counter(
            CAS_CONFLICT_TOTAL,
            "Total CAS (optimistic lock) conflicts",
            ["entity"],
            registry=self.registry,
        )
        self._mcp_circuit_state = Gauge(
            MCP_CIRCUIT_STATE,
            "MCP server circuit breaker state (0=closed, 1=open)",
            ["server"],
            registry=self.registry,
        )
        self._approval_pending_count = Gauge(
            APPROVAL_PENDING_COUNT,
            "Number of pending approval requests",
            registry=self.registry,
        )

    # ------------------------------------------------------------------
    # 事件处理
    # ------------------------------------------------------------------
    async def consume(self, event: SessionEvent) -> None:
        try:
            name = event.event_name
            payload = event.payload or {}
            if name == "task.created":
                # 新建任务 → pending
                self._tasks_total.labels(task_status="pending").inc()
            elif name in ("task.claimed", "task.in_progress"):
                # 抢占 → in_progress
                self._tasks_total.labels(task_status="in_progress").inc()
            elif name == "task.completed":
                self._tasks_total.labels(task_status="completed").inc()
            elif name == "task.failed":
                self._tasks_total.labels(task_status="failed").inc()
            elif name == "task.blocked":
                self._tasks_total.labels(task_status="blocked").inc()
            elif name == "llm.call_completed":
                provider = str(payload.get("provider", "unknown"))
                model = str(payload.get("model", "unknown"))
                kind = str(payload.get("kind", "prompt"))
                tokens = int(payload.get("tokens", 0))
                if tokens > 0:
                    self._llm_tokens_total.labels(
                        provider=provider, model=model, kind=kind,
                    ).inc(tokens)
            elif name == "cas.conflict":
                entity = str(payload.get("entity", "unknown"))
                self._cas_conflict_total.labels(entity=entity).inc()
            elif name == "mcp.circuit_changed":
                server = str(payload.get("server", "unknown"))
                state = str(payload.get("state", "closed"))
                # 0=closed, 1=open, 0.5=half_open
                val = 0.0 if state == "closed" else (1.0 if state == "open" else 0.5)
                self._mcp_circuit_state.labels(server=server).set(val)
            elif name == "approval.requested":
                self._approval_pending_count.inc()
            elif name == "approval.resolved":
                self._approval_pending_count.dec()
        except Exception as exc:  # noqa: BLE001
            log.warning("PrometheusSink consume failed: event=%s err=%s",
                        event.event_name, exc)

    # ------------------------------------------------------------------
    # /metrics HTTP 端点
    # ------------------------------------------------------------------
    async def metrics_handler(self, _request: web.Request) -> web.Response:
        """GET /metrics 端点——返回 Prometheus text format"""
        _require_prometheus()
        body = generate_latest(self.registry)
        # aiohttp 3.14+ 不接受 content_type 里含 charset；只传 mime + 单独 charset
        # CONTENT_TYPE_LATEST 形如 "text/plain; version=0.0.4; charset=utf-8"
        return web.Response(
            body=body,
            content_type="text/plain",
            charset="utf-8",
            headers={"X-Prometheus-Version": "0.0.4"},
        )

    async def healthz_handler(self, _request: web.Request) -> web.Response:
        """GET /healthz 端点——k8s 风格探活"""
        return web.Response(text="ok", content_type="text/plain")

    def make_app(self, path: str = "/metrics") -> web.Application:
        """
        构造独立 aiohttp app——可挂到主服务端口的子路径
        @note 完整端点：GET /metrics + GET /healthz
        """
        app = web.Application()
        app.router.add_get(path, self.metrics_handler)
        app.router.add_get("/healthz", self.healthz_handler)
        return app

    async def start_http_server(
        self, host: str = "0.0.0.0", port: int = 9090, path: str = "/metrics",
    ) -> tuple[web.AppRunner, web.TCPSite]:
        """
        启动独立 HTTP server 暴露 /metrics

        @return (runner, site) —— 调用方负责 keep + cleanup
        """
        app = self.make_app(path=path)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, host=host, port=port)
        await site.start()
        log.info("PrometheusSink /metrics listening on http://%s:%d%s",
                 host, port, path)
        return runner, site

    async def stop_http_server(
        self, runner: web.AppRunner, site: web.TCPSite,
    ) -> None:
        """关闭 HTTP server"""
        await site.stop()
        await runner.cleanup()

    # ------------------------------------------------------------------
    # 便捷 API——业务模块直接调，不必 emit 事件
    # ------------------------------------------------------------------
    def inc_task(self, task_status: str) -> None:
        """直接增加任务状态计数（绕过事件 emit）"""
        if task_status not in TASK_STATUS_LABELS:
            log.warning("PrometheusSink.inc_task: unknown status %r", task_status)
            return
        self._tasks_total.labels(task_status=task_status).inc()

    def add_llm_tokens(
        self, provider: str, model: str, kind: str, tokens: int,
    ) -> None:
        """直接增加 LLM token 计数"""
        if tokens <= 0:
            return
        self._llm_tokens_total.labels(
            provider=provider, model=model, kind=kind,
        ).inc(tokens)

    def inc_cas_conflict(self, entity: str) -> None:
        """直接增加 CAS 冲突计数"""
        self._cas_conflict_total.labels(entity=entity).inc()

    def set_mcp_circuit(self, server: str, state: str) -> None:
        """直接设置 MCP 熔断状态"""
        val = 0.0 if state == "closed" else (1.0 if state == "open" else 0.5)
        self._mcp_circuit_state.labels(server=server).set(val)

    def inc_approval_pending(self) -> None:
        self._approval_pending_count.inc()

    def dec_approval_pending(self) -> None:
        self._approval_pending_count.dec()


# 工具函数
async def run_metrics_server_forever(
    sink: PrometheusSink,
    host: str = "0.0.0.0",
    port: int = 9090,
) -> None:
    """便利函数：跑 metrics server 直到被取消"""
    runner, site = await sink.start_http_server(host=host, port=port)
    try:
        # 永久 sleep 直到取消
        await asyncio.Event().wait()
    finally:
        await sink.stop_http_server(runner, site)


__all__ = [
    "APPROVAL_PENDING_COUNT",
    "CAS_CONFLICT_TOTAL",
    "LLM_TOKENS_TOTAL",
    "MCP_CIRCUIT_STATE",
    "PrometheusSink",
    "TASKS_TOTAL",
    "TASK_STATUS_LABELS",
    "run_metrics_server_forever",
]
