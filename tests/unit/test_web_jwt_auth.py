"""
@module tests.unit.test_web_jwt_auth
@brief  P5-W34 JWT 鉴权单测 (≥15 cases) + G-024 Golden Case

覆盖:
  - JWTIssuer: encode/decode/expired/wrong-secret/tampered/algorithm check
  - resolve_secret_ref: ${VAR} 解析 + 字面值穿透
  - create_app: secret 缺省时无鉴权 / secret 给出时挂 middleware
  - middleware: Bearer 解析 / 错误 token 容错 / 无 token 不抛
  - Depends: get_current_user / require_user
  - G-024: login → 持 token 调受保护 API → 401 不带 token
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agent_swarm.web import WebState, create_app
from agent_swarm.web.auth import (
    JWTConfig,
    JWTError,
    JWTIssuer,
    get_current_user,
    require_user,
    resolve_secret_ref,
)

SECRET = "test-secret-do-not-use-in-prod"


# ---------------------------------------------------------------------------
# JWTIssuer 单元
# ---------------------------------------------------------------------------


def test_issuer_encode_decode_roundtrip() -> None:
    iss = JWTIssuer(JWTConfig(secret=SECRET))
    token = iss.encode("user-1", {"role": "admin"})
    claims = iss.decode(token)
    assert claims["sub"] == "user-1"
    assert claims["role"] == "admin"
    assert claims["iss"] == "agent-swarm"
    assert "iat" in claims
    assert "exp" in claims


def test_issuer_token_has_three_parts() -> None:
    iss = JWTIssuer(JWTConfig(secret=SECRET))
    token = iss.encode("u")
    assert token.count(".") == 2


def test_issuer_wrong_secret_raises() -> None:
    iss_a = JWTIssuer(JWTConfig(secret="secret-a"))
    iss_b = JWTIssuer(JWTConfig(secret="secret-b"))
    token = iss_a.encode("u")
    with pytest.raises(JWTError, match="signature"):
        iss_b.decode(token)


def test_issuer_expired_token_raises() -> None:
    iss = JWTIssuer(JWTConfig(secret=SECRET, expires_seconds=1))
    token = iss.encode("u")
    time.sleep(1.5)
    with pytest.raises(JWTError, match="expired"):
        iss.decode(token)


def test_issuer_tampered_payload_raises() -> None:
    iss = JWTIssuer(JWTConfig(secret=SECRET))
    token = iss.encode("user-1")
    # 替换 payload 段 (中间一段)
    parts = token.split(".")
    parts[1] = parts[1][:-2] + "AA"
    tampered = ".".join(parts)
    with pytest.raises(JWTError, match="signature"):
        iss.decode(tampered)


def test_issuer_empty_token_raises() -> None:
    iss = JWTIssuer(JWTConfig(secret=SECRET))
    with pytest.raises(JWTError, match="empty"):
        iss.decode("")


def test_issuer_garbage_token_raises() -> None:
    iss = JWTIssuer(JWTConfig(secret=SECRET))
    with pytest.raises(JWTError, match="3 parts"):
        iss.decode("not.a.valid.jwt.token.too.many")


def test_issuer_unsupported_alg_raises() -> None:
    """HS256-only: 即使伪造 alg=none header 也应拒绝"""
    import base64
    import json

    # 手工构造 alg=none 的 token
    header = base64.urlsafe_b64encode(
        json.dumps({"alg": "none", "typ": "JWT"}).encode()
    ).rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(
        json.dumps({"sub": "u", "exp": int(time.time()) + 3600}).encode()
    ).rstrip(b"=").decode()
    forged = f"{header}.{payload}."
    iss = JWTIssuer(JWTConfig(secret=SECRET))
    # alg 不匹配 → 验签时因为 alg 不同算出不同 sig → 仍被拒
    with pytest.raises(JWTError):
        iss.decode(forged)


def test_issuer_secret_required() -> None:
    """空 secret 时构造 JWTIssuer 抛 ValueError"""
    with pytest.raises(ValueError, match="secret"):
        JWTIssuer(JWTConfig(secret=""))


# ---------------------------------------------------------------------------
# resolve_secret_ref
# ---------------------------------------------------------------------------


def test_resolve_secret_ref_literal() -> None:
    """非 ${VAR} 形式原样返回"""
    assert resolve_secret_ref("plain-value") == "plain-value"
    assert resolve_secret_ref("not-a-ref") == "not-a-ref"


def test_resolve_secret_ref_from_env() -> None:
    env = {"MY_SECRET": "abc123"}
    assert resolve_secret_ref("${MY_SECRET}", env=env) == "abc123"


def test_resolve_secret_ref_missing_var_raises() -> None:
    with pytest.raises(ValueError, match="not set"):
        resolve_secret_ref("${MISSING_VAR}", env={})


# ---------------------------------------------------------------------------
# create_app + middleware 集成
# ---------------------------------------------------------------------------


def _client(jwt_secret: str | None = None) -> TestClient:
    app = create_app(web_state=WebState(), jwt_secret=jwt_secret)
    return TestClient(app)


def test_create_app_no_secret_means_no_auth() -> None:
    """未配 secret: middleware 不挂, 所有路由无需 token (zero-break)"""
    client = _client(jwt_secret=None)
    # POST 不带 token 应成功 (开发模式)
    r = client.post("/api/events", json={"event_name": "e", "session_id": "s"})
    assert r.status_code == 200


def test_create_app_with_secret_means_auth_required() -> None:
    """配了 secret: POST /api/events 不带 token 应 401"""
    client = _client(jwt_secret=SECRET)
    r = client.post("/api/events", json={"event_name": "e", "session_id": "s"})
    assert r.status_code == 401


def test_create_app_with_secret_dollar_ref() -> None:
    """配 ${ENV_VAR}: 应从 env 解析"""
    import os
    os.environ["MY_JWT_SECRET"] = "from-env"
    try:
        app = create_app(web_state=WebState(), jwt_secret="${MY_JWT_SECRET}")
        client = TestClient(app)
        r = client.post("/api/events", json={"event_name": "e", "session_id": "s"})
        assert r.status_code == 401
    finally:
        del os.environ["MY_JWT_SECRET"]


def test_middleware_parses_valid_bearer() -> None:
    """中间件解析合法 Bearer, 注入 request.state.user"""
    client = _client(jwt_secret=SECRET)
    iss = JWTIssuer(JWTConfig(secret=SECRET))
    token = iss.encode("user-1", {"role": "admin"})
    r = client.post(
        "/api/events",
        json={"event_name": "e", "session_id": "s"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["by"] == "user-1"


def test_middleware_invalid_bearer_treated_as_no_auth() -> None:
    """中间件对错误 token 容错 (不抛), 401 由 require_user 决定"""
    client = _client(jwt_secret=SECRET)
    r = client.post(
        "/api/events",
        json={"event_name": "e", "session_id": "s"},
        headers={"Authorization": "Bearer not.a.token"},
    )
    assert r.status_code == 401


def test_middleware_non_bearer_scheme_ignored() -> None:
    """Authorization: Basic / 不带 Bearer 前缀 → 当无 token"""
    client = _client(jwt_secret=SECRET)
    r = client.post(
        "/api/events",
        json={"event_name": "e", "session_id": "s"},
        headers={"Authorization": "Basic dXNlcjpwYXNz"},
    )
    assert r.status_code == 401


def test_get_endpoint_does_not_require_auth() -> None:
    """GET /api/state 不强制鉴权 (读操作开放, 与 W34 决策一致)"""
    client = _client(jwt_secret=SECRET)
    r = client.get("/api/state")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Depends 工具
# ---------------------------------------------------------------------------


def test_get_current_user_returns_none_when_unauthed() -> None:
    """无 secret 时 get_current_user 始终 None"""
    from starlette.requests import Request

    req = Request({"type": "http"})
    req.state.user = None
    assert get_current_user(req) is None


def test_require_user_raises_401_when_unauthed() -> None:
    """require_user 在 user=None 时抛 401"""
    from fastapi import HTTPException
    from starlette.requests import Request

    req = Request({"type": "http"})
    req.state.user = None
    with pytest.raises(HTTPException) as exc:
        require_user(req)
    assert exc.value.status_code == 401


# ---------------------------------------------------------------------------
# G-024 Golden Case: 鉴权端到端
# ---------------------------------------------------------------------------


def test_g024_login_then_protected_endpoint() -> None:
    """
    G-024 Golden Case:
      1) 用户用合法 secret 拿到 token
      2) 持 token POST /api/events → 200
      3) 不带 token POST /api/events → 401
      4) 带过期 token → 401
      5) 带错密钥 token → 401
    """
    client = _client(jwt_secret=SECRET)
    iss = JWTIssuer(JWTConfig(secret=SECRET))

    # 1+2) 合法 token
    good_token = iss.encode("alice", {"role": "writer"})
    r = client.post(
        "/api/events",
        json={"event_name": "task_done", "session_id": "s-1"},
        headers={"Authorization": f"Bearer {good_token}"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["by"] == "alice"

    # 3) 不带 token
    r = client.post("/api/events", json={"event_name": "x", "session_id": "s"})
    assert r.status_code == 401

    # 4) 过期 token
    iss_short = JWTIssuer(JWTConfig(secret=SECRET, expires_seconds=1))
    expired = iss_short.encode("bob")
    time.sleep(1.5)
    r = client.post(
        "/api/events",
        json={"event_name": "x", "session_id": "s"},
        headers={"Authorization": f"Bearer {expired}"},
    )
    assert r.status_code == 401

    # 5) 错密钥 token
    iss_evil = JWTIssuer(JWTConfig(secret="wrong-secret"))
    evil_token = iss_evil.encode("mallory")
    r = client.post(
        "/api/events",
        json={"event_name": "x", "session_id": "s"},
        headers={"Authorization": f"Bearer {evil_token}"},
    )
    assert r.status_code == 401
