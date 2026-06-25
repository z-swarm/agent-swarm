"""
@module tools.verify_w42_dod
@brief  P6-W42 DoD 守门脚本—8 项校验

W42 Plan §2 DoD:
  1. KB cache TTL 边界修复 (test_cache_ttl_expiry 通过)
  2. test_p01_sandbox_execute_strips_secret_env skipif win32
  3. test_p01_sandbox_env_overrides_not_redacted skipif win32
  4. test_tui_handles_very_many_agents_without_crash xfail win32
  5. ruff 0 / mypy 0 (W42 范围)
  6. 全量 tests/unit 不新增 fail (W36f 历史 fail 透明记录)
  7. knowledge_base.py cache TTL 边界代码已修 (>= not >)
  8. 4 个零信任 fail 全部处理 (1 修 / 2 skip / 1 xfail)

用法:
  .venv-win/Scripts/python tools/verify_w42_dod.py
  exit 0 = 全部通过
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


def check_1_kb_ttl_pass() -> bool:
    """1. test_cache_ttl_expiry PASS (W42 真修复)"""
    r = _run([
        sys.executable, "-m", "pytest",
        "tests/unit/test_knowledge_base.py::test_cache_ttl_expiry",
        "-q", "--no-header",
    ], cwd=str(ROOT))
    if "1 passed" not in r.stdout:
        print(f"  [1/8] test_cache_ttl_expiry: FAIL\n{r.stdout}\n{r.stderr}")
        return False
    print("  [1/8] test_cache_ttl_expiry PASS: OK")
    return True


def check_2_p01_execute_skipif() -> bool:
    """2. test_p01_sandbox_execute_strips_secret_env 在 Windows 上 SKIPPED"""
    r = _run([
        sys.executable, "-m", "pytest",
        "tests/unit/test_sandbox.py::test_p01_sandbox_execute_strips_secret_env",
        "-v", "--no-header",
    ], cwd=str(ROOT))
    if "SKIPPED" not in r.stdout:
        print(f"  [2/8] test_p01_sandbox_execute_strips_secret_env: FAIL (应 skip)\n{r.stdout}")
        return False
    print("  [2/8] test_p01_sandbox_execute_strips_secret_env SKIPPED: OK")
    return True


def check_3_p01_overrides_skipif() -> bool:
    """3. test_p01_sandbox_env_overrides_not_redacted 在 Windows 上 SKIPPED"""
    r = _run([
        sys.executable, "-m", "pytest",
        "tests/unit/test_sandbox.py::test_p01_sandbox_env_overrides_not_redacted",
        "-v", "--no-header",
    ], cwd=str(ROOT))
    if "SKIPPED" not in r.stdout:
        print(f"  [3/8] test_p01_sandbox_env_overrides_not_redacted: FAIL (应 skip)\n{r.stdout}")
        return False
    print("  [3/8] test_p01_sandbox_env_overrides_not_redacted SKIPPED: OK")
    return True


def check_4_tui_xfail() -> bool:
    """4. test_tui_handles_very_many_agents_without_crash 在 Windows 上 XFAIL"""
    r = _run([
        sys.executable, "-m", "pytest",
        "tests/unit/test_tui.py::test_tui_handles_very_many_agents_without_crash",
        "-v", "--no-header",
    ], cwd=str(ROOT))
    if "XFAIL" not in r.stdout:
        print(f"  [4/8] test_tui_handles_very_many_agents_without_crash: FAIL (应 xfail)\n{r.stdout}")
        return False
    print("  [4/8] test_tui_handles_very_many_agents_without_crash XFAIL: OK")
    return True


def check_5_ruff_mypy() -> bool:
    """5. ruff + mypy 0 (W42 范围: src + W42 改的 test 文件)"""
    r_ruff = _run([sys.executable, "-m", "ruff", "check", "src"], cwd=str(ROOT))
    r_mypy = _run([sys.executable, "-m", "mypy", "src/agent_swarm"], cwd=str(ROOT))
    if r_ruff.returncode != 0:
        print(f"  [5/8] ruff src: FAIL\n{r_ruff.stdout[:500]}")
        return False
    if r_mypy.returncode != 0:
        print(f"  [5/8] mypy: FAIL\n{r_mypy.stdout[:500]}")
        return False
    # tests/ 历史 import order (W41 时代) 不属 W42 scope, 仅校验 W42 修改的 3 文件
    w42_files = [
        "tests/unit/test_knowledge_base.py",
        "tests/unit/test_sandbox.py",
        "tests/unit/test_tui.py",
    ]
    r_ruff_test = _run([sys.executable, "-m", "ruff", "check", *w42_files], cwd=str(ROOT))
    if r_ruff_test.returncode != 0:
        print(f"  [5/8] ruff W42 test files: FAIL\n{r_ruff_test.stdout[:500]}")
        return False
    print("  [5/8] ruff + mypy (src + W42 测试文件): OK")
    return True


def check_6_no_new_fail() -> bool:
    """6. 全量 tests/unit 不新增 fail (已知 W36f 1 个 fail 透明记录)
    @note 全量跑约 90s, 设 240s timeout; 用 timeout=Popen, 不用 capture_output
    """
    import time as _t
    proc = subprocess.Popen(
        [sys.executable, "-m", "pytest", "tests/unit", "-q", "--no-header"],
        cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    t0 = _t.monotonic()
    out, _ = proc.communicate(timeout=240)
    elapsed = _t.monotonic() - t0
    m = re.search(r"(\d+) failed", out)
    if not m:
        print(f"  [6/8] no_new_fail: UNKNOWN (no match, {elapsed:.1f}s)\n{out[-500:]}")
        return False
    fail_count = int(m.group(1))
    if fail_count > 1:
        print(f"  [6/8] no_new_fail: FAIL ({fail_count} failed, 期望 ≤1 W36f 已知, {elapsed:.1f}s)\n{out[-500:]}")
        return False
    print(f"  [6/8] no_new_fail: OK ({fail_count} known W36f fail, {elapsed:.1f}s)")
    return True


def check_7_kb_code_fixed() -> bool:
    """7. knowledge_base.py cache TTL 边界已用 >= not >"""
    fp = ROOT / "src" / "agent_swarm" / "core" / "knowledge_base.py"
    text = fp.read_text(encoding="utf-8")
    if "time.time() >= entry.expires_at" not in text:
        print("  [7/8] knowledge_base.py code fix: NOT FOUND")
        return False
    if "time.time() > entry.expires_at" in text:
        print("  [7/8] knowledge_base.py code fix: 残留 > 旧写法")
        return False
    print("  [7/8] knowledge_base.py cache TTL >= 边界: OK")
    return True


def check_8_zero_trust_fail_handled() -> bool:
    """8. 4 个零信任 fail 全部处理 (1 修 / 2 skip / 1 xfail)"""
    sandbox = (ROOT / "tests/unit/test_sandbox.py").read_text(encoding="utf-8")
    tui = (ROOT / "tests/unit/test_tui.py").read_text(encoding="utf-8")
    if sandbox.count('@pytest.mark.skipif(\n    sys.platform == "win32"') < 2:
        # 计数不严格 (允许多种格式), 简化: 看 skipif 关键字是否在 p01 测试前
        if "P3-WIN: Windows 没有 printenv" not in sandbox:
            print("  [8/8] sandbox P3-WIN skipif: 缺失")
            return False
    if "test_tui_handles_very_many_agents_without_crash" not in tui:
        print("  [8/8] tui xfail: 缺失")
        return False
    if "@pytest.mark.xfail" not in tui:
        print("  [8/8] tui xfail 标记: 缺失")
        return False
    if "test_cache_ttl_expiry" not in (ROOT / "tests/unit/test_knowledge_base.py").read_text(encoding="utf-8"):
        print("  [8/8] knowledge_base TTL 测试: 缺失")
        return False
    print("  [8/8] 4 零信任 fail 全部处理 (1 修 + 2 skip + 1 xfail): OK")
    return True


def main() -> int:
    print("=" * 60)
    print("P6-W42 DoD 守门 (8 项)")
    print("=" * 60)
    checks = [
        check_1_kb_ttl_pass,
        check_2_p01_execute_skipif,
        check_3_p01_overrides_skipif,
        check_4_tui_xfail,
        check_5_ruff_mypy,
        check_6_no_new_fail,
        check_7_kb_code_fixed,
        check_8_zero_trust_fail_handled,
    ]
    failed = 0
    for c in checks:
        try:
            if not c():
                failed += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  [ERROR] {c.__name__}: {exc}")
            failed += 1
    print("=" * 60)
    if failed == 0:
        print("ALL PASS — W42 DoD 8/8")
        return 0
    print(f"FAILED {failed}/8")
    return 1


if __name__ == "__main__":
    sys.exit(main())
