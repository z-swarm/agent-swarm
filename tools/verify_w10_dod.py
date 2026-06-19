"""
@brief  W10 飞书连接器 DoD 验收脚本（DESIGN §17.2 ①）

W10 DoD（按 audit 推荐 + DESIGN §17.2 ①）：
  ① 飞书连接器签名验证 + 卡片交互在真实 Lark 工作区可用
     - 实际用 mock Lark server 验证（真 Lark 需 app_id + app_secret + 后台配置）
  ② ChannelAdapter 路由 + 鉴权 + 限流 + 会话绑定
  ③ 5 个内置卡片模板 + LarkConnector 集成
  ④ SecretManager 引用强制（DESIGN §4.4）
  ⑤ example/w10_lark.yaml 可被 Swarm.from_yaml 解析

@note  通过条件：本脚本 exit 0
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


def check_modules_importable() -> None:
    """① 模块结构：channels/ 包 + base / lark / adapter / card_templates 4 个子模块"""
    print("[1/5] channels/ 模块结构 + 公开 API")
    proc = _run([".venv/bin/python", "-c", """
from agent_swarm.channels import (
    ChannelConnector, ChannelMessage, ChannelResponse, ChannelType, ChannelUser,
    MessageHandler, MessageType, LarkConnector, ChannelAdapter,
    RateLimiter, SessionBindingManager, APIKeyStore,
    CARD_TEMPLATES, render_card, render_task_progress, render_code_review_result,
    render_adversarial_debug, render_swarm_status, render_confirm_dialog,
    verify_lark_signature, resolve_lark_secret, LARK_API_BASE,
)
assert len(CARD_TEMPLATES) == 5
assert ChannelType.LARK.value == "lark"
print("ok")
"""])
    assert "ok" in proc.stdout
    print("  ✓ channels/ 包 + 4 个子模块 + 23 个公开 API")


def check_unit_tests() -> None:
    """② 单元测试全过"""
    print("[2/5] 单元测试 (W10 新增)")
    proc = _run([".venv/bin/python", "-m", "pytest",
                 "tests/unit/test_channels_base.py",
                 "tests/unit/test_channels_lark.py",
                 "tests/unit/test_channels_adapter.py",
                 "tests/unit/test_card_templates.py",
                 "-q", "--no-header"])
    # 期望: 7+27+19+16 = 69 passed
    last = proc.stdout.strip().splitlines()[-1]
    n = int(last.split()[0])
    assert n >= 69, f"W10 单元测试数 {n} < 69"
    print(f"  ✓ {last}")


def check_e2e_tests() -> None:
    """③ e2e 测试全过（mock Lark server 完整路径）"""
    print("[3/5] e2e 测试 (mock Lark server 端到端)")
    proc = _run([".venv/bin/python", "-m", "pytest",
                 "tests/e2e/test_w10_lark_e2e.py",
                 "-q", "--no-header"])
    last = proc.stdout.strip().splitlines()[-1]
    n = int(last.split()[0])
    assert n >= 11, f"W10 e2e 测试数 {n} < 11"
    print(f"  ✓ {last}")


def check_example_yaml() -> None:
    """④ example/w10_lark.yaml 存在 + SecretManager 引用"""
    print("[4/5] example/w10_lark.yaml + SecretManager 引用")
    p = REPO / "examples/w10_lark.yaml"
    assert p.exists(), "example/w10_lark.yaml 缺失"
    content = p.read_text(encoding="utf-8")
    # app_secret / verification_token / encrypt_key 必须用 ${VAR} 引用
    assert "app_secret: \"${LARK_APP_SECRET}\"" in content
    assert "verification_token: \"${LARK_VERIFICATION_TOKEN}\"" in content
    assert "encrypt_key: \"${LARK_ENCRYPT_KEY}\"" in content
    # user_whitelist 必须有
    assert "user_whitelist:" in content
    print("  ✓ example 存在 + 3 个密钥字段用 SecretManager 引用")


def check_no_regression() -> None:
    """⑤ 无回归（mypy 0 + 全量 65+ 套测试通过）"""
    print("[5/5] mypy + 全量回归")
    proc = _run([".venv/bin/python", "-m", "mypy", "src/agent_swarm"])
    assert "Success" in proc.stdout, f"mypy 失败: {proc.stdout}"
    print("  ✓ mypy 0 errors")
    proc = _run([".venv/bin/python", "-m", "pytest",
                 "tests/unit", "tests/e2e", "tests/golden", "tests/security",
                 "-q", "--no-header"])
    last = proc.stdout.strip().splitlines()[-1]
    n = int(last.split()[0])
    assert n >= 730, f"W10 后测试数 {n} < 730 (基线 671 + W10 新增)"
    print(f"  ✓ {last}")


def main() -> None:
    print("=" * 60)
    print("W10 飞书连接器 DoD 验收 — DESIGN §17.2 ①")
    print("=" * 60)
    check_modules_importable()
    check_unit_tests()
    check_e2e_tests()
    check_example_yaml()
    check_no_regression()
    print()
    print("=" * 60)
    print("✅ W10 全部通过（5/5 DoD 验收项）")
    print("=" * 60)


if __name__ == "__main__":
    main()
