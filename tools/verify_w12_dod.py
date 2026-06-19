"""
@brief  W12 WebSocketSink + 完整事件目录 DoD 验收脚本

W12 DoD（DESIGN §5.3 + §15 Phase 2）：
  ① WebSocketSink 启动 + 多客户端广播
  ② 完整事件目录（5 元组索引）
  ③ 心跳 / 断线重连 / 背压
  ④ ObservabilityBus 集成（emit() → WS）
  ⑤ 关闭时清理所有连接
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _run(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        cmd, cwd=cwd or REPO, capture_output=True, text=True, timeout=180
    )
    if proc.returncode != 0:
        sys.stderr.write(f"FAIL: {cmd}\n--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}\n")
        raise SystemExit(proc.returncode)
    return proc


def check_module_import() -> None:
    """① WebSocketSink 模块可导入 + 公开 API"""
    print("[1/5] WebSocketSink 模块 + 公开 API")
    proc = _run([".venv/bin/python", "-c", """
from agent_swarm.observability import (
    WebSocketSink, SqliteEventSink, JsonLogSink, InMemorySink,
    ObservabilityBus, ObservabilitySink, emit, get_global_bus, set_global_bus,
)
print("ok")
"""])
    assert "ok" in proc.stdout
    print("  ✓ WebSocketSink + 5 个 sink 全导出")


def check_unit_tests() -> None:
    """② 单元测试全过"""
    print("[2/5] W12 单元测试 (WebSocketSink)")
    proc = _run([".venv/bin/python", "-m", "pytest",
                 "tests/unit/test_websocket_sink.py",
                 "-q", "--no-header"])
    last = proc.stdout.strip().splitlines()[-1]
    n = int(last.split()[0])
    assert n >= 8, f"W12 单元测试 {n} < 8"
    print(f"  ✓ {last}")


def check_e2e_tests() -> None:
    """③ e2e 全过"""
    print("[3/5] W12 e2e")
    proc = _run([".venv/bin/python", "-m", "pytest",
                 "tests/e2e/test_w12_websocket_e2e.py",
                 "-q", "--no-header"])
    last = proc.stdout.strip().splitlines()[-1]
    n = int(last.split()[0])
    assert n >= 5, f"W12 e2e {n} < 5"
    print(f"  ✓ {last}")


def check_five_tuple_index() -> None:
    """④ SqliteEventSink 含 5 元组索引"""
    print("[4/5] SqliteEventSink 完整事件目录索引")
    src = (REPO / "src/agent_swarm/observability/sqlite_sink.py").read_text(encoding="utf-8")
    for idx in ["idx_events_5tuple", "idx_events_tenant_time", "idx_events_request_id"]:
        assert idx in src, f"缺索引 {idx}"
    print("  ✓ 5 元组 + 时间 + request_id 索引齐全")


def check_no_regression() -> None:
    """⑤ 无回归"""
    print("[5/5] mypy + 全量回归")
    proc = _run([".venv/bin/python", "-m", "mypy", "src/agent_swarm"])
    assert "Success" in proc.stdout
    print("  ✓ mypy 0 errors")
    proc = _run([".venv/bin/python", "-m", "pytest",
                 "tests/unit", "tests/e2e", "tests/golden", "tests/security",
                 "-q", "--no-header"])
    last = proc.stdout.strip().splitlines()[-1]
    n = int(last.split()[0])
    assert n >= 780, f"W12 后测试数 {n} < 780"
    print(f"  ✓ {last}")


def main() -> None:
    print("=" * 60)
    print("W12 WebSocketSink + 完整事件目录 DoD 验收")
    print("=" * 60)
    check_module_import()
    check_unit_tests()
    check_e2e_tests()
    check_five_tuple_index()
    check_no_regression()
    print()
    print("=" * 60)
    print("✅ W12 全部通过（5/5 DoD 验收项）")
    print("=" * 60)


if __name__ == "__main__":
    main()
