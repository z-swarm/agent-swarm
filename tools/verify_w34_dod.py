"""
@module tools.verify_w34_dod
@brief  P5-W34 DoD 守门脚本——8 项检查

P5-W34 Plan §4 Check 守门点:
  1. JWTIssuer 单元 (encode + decode roundtrip)
  2. JWT 签名验证 (错密钥拒绝)
  3. JWT 过期验证
  4. resolve_secret_ref ${VAR} 引用
  5. create_app 无 secret 零破坏 (POST 不带 token 200)
  6. create_app 有 secret 时 401 拦截 (写路径)
  7. CLI --web-jwt-secret / --web-jwt-expires 选项存在
  8. 性能基线 (100 token encode + decode < 1s)

用法:
  .venv/bin/python tools/verify_w34_dod.py
  exit 0 = 全部通过; 退出码 != 0 = 失败项
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


SECRET = "verify-w34-secret"


def _check(label: str, ok: bool, detail: str = "") -> bool:
    mark = "✓" if ok else "✗"
    print(f"  {mark} {label}{(' — ' + detail) if detail else ''}")
    return ok


def _check1_encode_decode_roundtrip() -> bool:
    """1) JWT encode + decode roundtrip"""
    from agent_swarm.web.auth import JWTConfig, JWTIssuer
    iss = JWTIssuer(JWTConfig(secret=SECRET))
    token = iss.encode("user-1", {"role": "admin"})
    claims = iss.decode(token)
    ok = (
        claims["sub"] == "user-1"
        and claims["role"] == "admin"
        and claims["iss"] == "agent-swarm"
    )
    return _check("JWT encode + decode roundtrip", ok, f"sub={claims.get('sub')}")


def _check2_wrong_secret_rejected() -> bool:
    """2) 错密钥拒绝"""
    from agent_swarm.web.auth import JWTConfig, JWTError, JWTIssuer
    iss_a = JWTIssuer(JWTConfig(secret="secret-a"))
    iss_b = JWTIssuer(JWTConfig(secret="secret-b"))
    token = iss_a.encode("u")
    try:
        iss_b.decode(token)
        return _check("错密钥拒绝", False, "expected raise")
    except JWTError as exc:
        return _check("错密钥拒绝", "signature" in str(exc), str(exc)[:60])


def _check3_expired_rejected() -> bool:
    """3) 过期 token 拒绝"""
    from agent_swarm.web.auth import JWTConfig, JWTError, JWTIssuer
    iss = JWTIssuer(JWTConfig(secret=SECRET, expires_seconds=1))
    token = iss.encode("u")
    time.sleep(1.5)
    try:
        iss.decode(token)
        return _check("过期 token 拒绝", False, "expected raise")
    except JWTError as exc:
        return _check("过期 token 拒绝", "expired" in str(exc), str(exc)[:60])


def _check4_resolve_secret_ref() -> bool:
    """4) resolve_secret_ref ${VAR} 解析"""
    from agent_swarm.web.auth import resolve_secret_ref
    env = {"MY_SECRET": "abc"}
    ok1 = resolve_secret_ref("${MY_SECRET}", env=env) == "abc"
    ok2 = resolve_secret_ref("plain") == "plain"
    return _check("${VAR} 解析 + 字面值穿透", ok1 and ok2, f"ref={ok1} literal={ok2}")


def _check5_no_secret_zero_break() -> bool:
    """5) 无 secret: POST 不带 token 200 (零破坏)"""
    from fastapi.testclient import TestClient
    from agent_swarm.web import WebState, create_app
    app = create_app(web_state=WebState(), jwt_secret=None)
    client = TestClient(app)
    r = client.post("/api/events", json={"event_name": "e", "session_id": "s"})
    return _check("无 secret 零破坏 (POST 200)", r.status_code == 200, f"status={r.status_code}")


def _check6_secret_required_401() -> bool:
    """6) 有 secret: POST 不带 token 401"""
    from fastapi.testclient import TestClient
    from agent_swarm.web import WebState, create_app
    app = create_app(web_state=WebState(), jwt_secret=SECRET)
    client = TestClient(app)
    r = client.post("/api/events", json={"event_name": "e", "session_id": "s"})
    return _check("有 secret 401 拦截", r.status_code == 401, f"status={r.status_code}")


def _check7_cli_options() -> bool:
    """7) CLI --web-jwt-secret / --web-jwt-expires"""
    from click.testing import CliRunner
    from agent_swarm.cli.main import cli
    runner = CliRunner()
    result = runner.invoke(cli, ["run", "--help"])
    out = result.output
    ok1 = "--web-jwt-secret" in out
    ok2 = "--web-jwt-expires" in out
    return _check("CLI --web-jwt-secret/--web-jwt-expires", ok1 and ok2, f"secret={ok1} expires={ok2}")


def _check8_perf_baseline() -> bool:
    """8) 100 token encode + decode < 1s"""
    from agent_swarm.web.auth import JWTConfig, JWTIssuer
    iss = JWTIssuer(JWTConfig(secret=SECRET))
    t0 = time.monotonic()
    for i in range(100):
        tok = iss.encode(f"u{i}")
        iss.decode(tok)
    elapsed = time.monotonic() - t0
    ok = elapsed < 1.0
    return _check("性能基线 (100 encode+decode < 1s)", ok, f"{elapsed*1000:.1f}ms")


def main() -> int:
    print("=" * 60)
    print("P5-W34 DoD 守门 (8 项)")
    print("=" * 60)
    results: list[bool] = [
        _check1_encode_decode_roundtrip(),
        _check2_wrong_secret_rejected(),
        _check3_expired_rejected(),
        _check4_resolve_secret_ref(),
        _check5_no_secret_zero_break(),
        _check6_secret_required_401(),
        _check7_cli_options(),
        _check8_perf_baseline(),
    ]
    passed = sum(results)
    total = len(results)
    print("=" * 60)
    if passed == total:
        print(f"✅ P5-W34 全部通过 ({passed}/{total})")
        return 0
    print(f"❌ P5-W34 部分失败 ({passed}/{total})")
    return 1


if __name__ == "__main__":
    sys.exit(main())
