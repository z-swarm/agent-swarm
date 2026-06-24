"""
@file tools/multi_worker_smoke.py
@brief W41 多 worker 部署实战 smoke 脚本

3 场景:
  1. uvicorn workers=N 工厂模式启动 — 验证 CLI 多 worker 启动
  2. 单 worker HTTP 端到端 — 验证基础路径
  3. 干净退出 — SIGTERM → uvicorn 干净退出

不依赖外部服务 (无真 Redis, 无 Postgres), 仅验证 uvicorn 多 worker 启动 + HTTP 路径。
跨 worker SSE 通知实战验证见 tests/e2e/test_w41_multi_worker_e2e.py (单进程 2 app 实例 + 共享 fakeredis)。

用法:
  .venv/bin/python tools/multi_worker_smoke.py
  exit 0 = 全过, exit 1 = 有失败
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV_PYTHON = str(ROOT / ".venv" / "bin" / "python")


def _ok(msg: str) -> None:
    print(f"[OK] {msg}")


def _fail(msg: str) -> None:
    print(f"[FAIL] {msg}")


def _wait_for_http(url: str, timeout: float = 15.0) -> bool:
    """等 URL 200 (max timeout 秒)"""
    import urllib.request

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as resp:  # noqa: S310
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(0.2)
    return False


def scenario_uvicorn_workers_2() -> bool:
    """场景 1: uvicorn workers=2 启动 + 干净退出"""
    print("\n=== 场景 1: uvicorn workers=2 工厂模式启动 ===")
    port = 18001
    env = os.environ.copy()
    # factory 模式从 env 读配置
    env["WEB_POSTGRES_DSN"] = ""
    env["WEB_REDIS_DSN"] = ""
    env["WEB_TASK_STORE"] = "memory"  # 单实例测试, 不连 Redis
    proc = subprocess.Popen(  # noqa: S603
        [
            VENV_PYTHON, "-m", "uvicorn",
            "agent_swarm.web:app_factory",
            "--host", "127.0.0.1",
            "--port", str(port),
            "--workers", "2",
            "--log-level", "warning",
        ],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        # 等服务可用
        if not _wait_for_http(f"http://127.0.0.1:{port}/healthz", timeout=20.0):
            _fail(f"uvicorn workers=2 启动失败 (port {port} 不通)")
            if proc.stdout:
                print("--- uvicorn 输出 ---")
                print(proc.stdout.read().decode("utf-8", errors="replace")[:2000])
            return False
        _ok(f"uvicorn workers=2 启动成功 (port {port}, /healthz 200)")
        # 测两个端点 (1 次响应可能由任一 worker 处理)
        import urllib.request
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2.0) as r:  # noqa: S310
            if r.status != 200:
                _fail(f"/healthz 状态码 {r.status} != 200")
                return False
            _ok("/healthz 200 OK")
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=2.0) as r:  # noqa: S310
            if r.status != 200:
                _fail(f"/api/state 状态码 {r.status} != 200")
                return False
            _ok("/api/state 200 OK (任一 worker 响应)")
        return True
    finally:
        # 干净退出: SIGTERM
        proc.terminate()
        try:
            proc.wait(timeout=5.0)
            _ok("uvicorn workers=2 SIGTERM 干净退出")
        except subprocess.TimeoutExpired:
            _fail("uvicorn 5s 内未退出, 强杀")
            proc.kill()
            return False


def scenario_single_worker_baseline() -> bool:
    """场景 2: 单 worker 端到端 (零破坏回归, W28 路径)"""
    print("\n=== 场景 2: 单 worker 端到端 (零破坏回归) ===")
    port = 18002
    env = os.environ.copy()
    env["WEB_POSTGRES_DSN"] = ""
    env["WEB_REDIS_DSN"] = ""
    env["WEB_TASK_STORE"] = "memory"
    proc = subprocess.Popen(  # noqa: S603
        [
            VENV_PYTHON, "-m", "uvicorn",
            "agent_swarm.web:app_factory",
            "--host", "127.0.0.1",
            "--port", str(port),
            "--workers", "1",
            "--log-level", "warning",
        ],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        if not _wait_for_http(f"http://127.0.0.1:{port}/healthz", timeout=15.0):
            _fail(f"单 worker 启动失败 (port {port})")
            if proc.stdout:
                print("--- uvicorn 输出 ---")
                print(proc.stdout.read().decode("utf-8", errors="replace")[:2000])
            return False
        _ok(f"单 worker 启动成功 (port {port})")
        # 测 review 端点 (POST /api/review 但 simple mode 跑不动,只验 status 端点)
        # 实际: 注入 task_store 后 status 端点 OK, 我们用 fake 方式: 直接用 curl 测 healthz + state
        import urllib.request
        for ep in ("/healthz", "/api/state", "/", "/partials/events"):
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{ep}", timeout=2.0) as r:  # noqa: S310
                if r.status != 200:
                    _fail(f"GET {ep} 状态码 {r.status} != 200")
                    return False
        _ok("单 worker 4 端点 (healthz / api/state / / / partials/events) 全 200")
        return True
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5.0)
            _ok("单 worker SIGTERM 干净退出")
        except subprocess.TimeoutExpired:
            _fail("单 worker 5s 内未退出")
            proc.kill()
            return False


def scenario_app_factory_no_args() -> bool:
    """场景 3: app_factory() 无参调用 (env 全缺省)"""
    print("\n=== 场景 3: app_factory() 无参 (env 缺省) ===")
    # 子进程: 清空 env, 调 app_factory
    code = (
        "from agent_swarm.web import app_factory, MemoryTaskStore; "
        "app = app_factory(); "
        "store = app.state.task_store; "
        "assert isinstance(store, MemoryTaskStore), 'got ' + type(store).__name__; "
        "print('OK app_factory no_args memory default')"
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("WEB_")}
    result = subprocess.run(  # noqa: S603
        [VENV_PYTHON, "-c", code],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=30.0,
    )
    if result.returncode != 0 or "OK" not in result.stdout:
        _fail(f"app_factory 无参失败: rc={result.returncode} stdout={result.stdout!r} stderr={result.stderr!r}")
        return False
    _ok("app_factory() 无参 → MemoryTaskStore (零破坏)")
    return True


def main() -> int:
    print("W41 多 worker 部署实战 smoke (3 场景)")
    results = [
        scenario_uvicorn_workers_2(),
        scenario_single_worker_baseline(),
        scenario_app_factory_no_args(),
    ]
    print()
    passed = sum(results)
    total = len(results)
    print(f"=== smoke: {passed}/{total} PASSED ===")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
