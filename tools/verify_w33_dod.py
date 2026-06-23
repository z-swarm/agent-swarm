"""
@module tools.verify_w33_dod
@brief  P5-W33 DoD 守门脚本——8 项检查

P5-W33 Plan §4 Check 守门点:
  1. 表创建 (Schema + 3 索引)
  2. append 写事件
  3. recent 拉回
  4. subscribe 通知
  5. 重启恢复 (G-023 基础)
  6. CLI 选项存在 (--web-postgres-dsn / --web-postgres-table)
  7. DSN 缺省降级 (无 DSN 时走内存)
  8. 性能基线 (100 事件 append 耗时)

用法:
  .venv/bin/python tools/verify_w33_dod.py
  exit 0 = 全部通过; 退出码 != 0 = 失败项
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from typing import Any

# 让 tools/ 可导入 src/agent_swarm
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


# ---------------------------------------------------------------------------
# Fake asyncpg (与 tests/unit/test_webstate_store.py 同模式, 简化版)
# ---------------------------------------------------------------------------


class _FakeConn:
    def __init__(self, store: list[dict[str, Any]], counter: list[int]) -> None:
        self.store = store
        self.counter = counter

    async def execute(self, sql: str, *args: Any) -> str:
        s = sql.lower().lstrip()
        if s.startswith("create "):
            return "OK"
        if s.startswith("insert into"):
            event_type, payload_json, session_id, tenant_id = args
            self.counter[0] += 1
            self.store.append({
                "seq": self.counter[0],
                "ts": 1.0 + self.counter[0] * 0.001,
                "event_type": event_type,
                "payload": payload_json,
                "session_id": session_id,
                "tenant_id": tenant_id,
            })
            return "INSERT 0 1"
        return "OK"

    async def fetch(self, sql: str, *args: Any) -> list[Any]:
        s = sql.lower().lstrip()
        if "where session_id" in s:
            session_id, n = args
            rows = sorted(
                (r for r in self.store if r["session_id"] == session_id),
                key=lambda r: r["ts"], reverse=True,
            )
            return [dict(r) for r in rows[:n]]
        if "from webstate_events" in s:
            n = args[0]
            sorted_rows = sorted(self.store, key=lambda r: r["ts"], reverse=True)
            return [dict(r) for r in sorted_rows[:n]]
        return []


class _Acquire:
    def __init__(self, store: list[dict[str, Any]], counter: list[int]) -> None:
        self.store = store
        self.counter = counter

    async def __aenter__(self) -> _FakeConn:
        return _FakeConn(self.store, self.counter)

    async def __aexit__(self, *args: Any) -> None:
        pass


class _FakePool:
    def __init__(self, store: list[dict[str, Any]], counter: list[int]) -> None:
        self.store = store
        self.counter = counter

    def acquire(self) -> _Acquire:
        return _Acquire(self.store, self.counter)


def _mk_fake_module() -> Any:
    state: dict[str, Any] = {"store": [], "counter": [0]}

    class FakeMod:
        @staticmethod
        async def create_pool(**kwargs: Any) -> _FakePool:
            return _FakePool(state["store"], state["counter"])

    FakeMod._state = state  # type: ignore[attr-defined]
    return FakeMod()


# ---------------------------------------------------------------------------
# 检查项
# ---------------------------------------------------------------------------


def _check(label: str, ok: bool, detail: str = "") -> bool:
    """@brief 单项检查 + 输出 + 返回 ok"""
    mark = "✓" if ok else "✗"
    print(f"  {mark} {label}{(' — ' + detail) if detail else ''}")
    return ok


async def _check1_schema_sql() -> bool:
    """1) Schema SQL 必含 6 列 + 3 索引"""
    from agent_swarm.web.store import SCHEMA_SQL
    cols = ("seq", "ts", "event_type", "payload", "session_id", "tenant_id")
    idxs = ("_ts_idx", "_session_seq_idx", "_tenant_ts_idx")
    ok = all(c in SCHEMA_SQL for c in cols) and all(i in SCHEMA_SQL for i in idxs)
    return _check("Schema SQL 含 6 列 + 3 索引", ok,
                  f"cols={sum(c in SCHEMA_SQL for c in cols)}/{len(cols)} "
                  f"idxs={sum(i in SCHEMA_SQL for i in idxs)}/{len(idxs)}")


async def _check2_append_writes() -> bool:
    """2) append 写入能持久化"""
    from agent_swarm.web.store import PostgresWebStateStore, WebStateConfig
    mod = _mk_fake_module()
    store = PostgresWebStateStore(WebStateConfig(dsn="postgresql://fake", fake_module=mod))
    await store.append("test-event", "s-1", 1, {"k": "v"})
    recs = await store.recent(10)
    ok = len(recs) == 1 and recs[0]["event_name"] == "test-event"
    return _check("append 写入持久化", ok, f"recs={len(recs)}")


async def _check3_recent_pullback() -> bool:
    """3) recent 拉回事件"""
    from agent_swarm.web.store import PostgresWebStateStore, WebStateConfig
    mod = _mk_fake_module()
    store = PostgresWebStateStore(WebStateConfig(dsn="postgresql://fake", fake_module=mod))
    for i in range(10):
        await store.append(f"e{i}", "s", i, {"i": i})
    recs = await store.recent(5)
    ok = len(recs) == 5 and [r["event_name"] for r in recs] == ["e9", "e8", "e7", "e6", "e5"]
    return _check("recent 拉回 (ORDER BY ts DESC)", ok, f"first={recs[0]['event_name'] if recs else None}")


async def _check4_subscribe_notify() -> bool:
    """4) subscribe 通知新事件"""
    from agent_swarm.web.store import PostgresWebStateStore, WebStateConfig
    mod = _mk_fake_module()
    store = PostgresWebStateStore(WebStateConfig(dsn="postgresql://fake", fake_module=mod))
    received: list[dict[str, Any]] = []

    async def cb(rec: dict[str, Any]) -> None:
        received.append(rec)

    store.subscribe(cb)
    await store.append("e", "s", 1, {})
    ok = len(received) == 1 and received[0]["event_name"] == "e"
    return _check("subscribe 通知 (单进程 fan-out)", ok, f"received={len(received)}")


async def _check5_recovery_g023() -> bool:
    """5) 重启恢复 (G-023 基础) — 同 fake 共享 store 模拟跨进程"""
    from agent_swarm.web.store import PostgresWebStateStore, WebStateConfig
    mod = _mk_fake_module()

    proc_a = PostgresWebStateStore(WebStateConfig(dsn="postgresql://fake", fake_module=mod))
    for i in range(5):
        await proc_a.append(f"e{i}", "s", i, {"i": i})
    await proc_a.close()

    proc_b = PostgresWebStateStore(WebStateConfig(dsn="postgresql://fake", fake_module=mod))
    recs = await proc_b.recent(50)
    ok = len(recs) == 5
    return _check("重启恢复 (G-023 基础)", ok, f"proc_b 拉回 {len(recs)} 条")


def _check6_cli_options() -> bool:
    """6) CLI --web-postgres-dsn / --web-postgres-table 选项存在"""
    from click.testing import CliRunner
    from agent_swarm.cli.main import cli
    runner = CliRunner()
    result = runner.invoke(cli, ["run", "--help"])
    out = result.output
    ok1 = "--web-postgres-dsn" in out
    ok2 = "--web-postgres-table" in out
    return _check(
        "CLI --web-postgres-dsn / --web-postgres-table",
        ok1 and ok2,
        f"dsn={ok1} table={ok2}",
    )


def _check7_dsn_degrades_to_memory() -> bool:
    """7) DSN 缺省降级 (create_app 无 postgres_dsn 时 store=None)"""
    from agent_swarm.web import WebState, create_app
    state = WebState()
    # 不传 postgres_dsn
    create_app(web_state=state)
    ok = state.store is None
    return _check("DSN 缺省降级内存", ok, f"store={state.store}")


async def _check8_perf_baseline() -> bool:
    """8) 性能基线: 100 事件 append < 1s (in-process fake)"""
    from agent_swarm.web.store import PostgresWebStateStore, WebStateConfig
    mod = _mk_fake_module()
    store = PostgresWebStateStore(WebStateConfig(dsn="postgresql://fake", fake_module=mod))
    t0 = time.monotonic()
    for i in range(100):
        await store.append(f"e{i}", "s", i, {"i": i})
    elapsed = time.monotonic() - t0
    ok = elapsed < 1.0
    return _check("性能基线 (100 append < 1s)", ok, f"{elapsed*1000:.1f}ms")


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


async def _run_all() -> int:
    print("=" * 60)
    print("P5-W33 DoD 守门 (8 项)")
    print("=" * 60)
    results: list[bool] = []
    results.append(await _check1_schema_sql())
    results.append(await _check2_append_writes())
    results.append(await _check3_recent_pullback())
    results.append(await _check4_subscribe_notify())
    results.append(await _check5_recovery_g023())
    results.append(_check6_cli_options())
    results.append(_check7_dsn_degrades_to_memory())
    results.append(await _check8_perf_baseline())

    passed = sum(results)
    total = len(results)
    print("=" * 60)
    if passed == total:
        print(f"✅ P5-W33 全部通过 ({passed}/{total})")
        return 0
    print(f"❌ P5-W33 部分失败 ({passed}/{total})")
    return 1


def main() -> int:
    return asyncio.run(_run_all())


if __name__ == "__main__":
    sys.exit(main())
