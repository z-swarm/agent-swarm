"""
@module agent_swarm.web.auth
@brief  P5-W34 JWT 鉴权——标准库 HS256 实现

DESIGN §17.2 P5-W34 DoD 拆解:
  - D1 JWTIssuer (encode/decode/verify_exp) + JWTConfig
  - D2 FastAPI middleware 解析 Authorization: Bearer
  - D3 Depends(get_current_user) + require_user (401 if not authed)

设计原则:
  - 标准库手写 HS256 (零新依赖; PyJWT 不在 [web] extras)
  - 默认 zero-break: 无 secret 时所有路由无需 token (开发模式)
  - 有 secret 时: middleware 解析但不强制; 关键 API 用 require_user 强制
  - ${VAR} 引用: secret 走 W20 SecretManager.get(secret_ref) 模式

格式: header.payload.signature
  - header = base64url({"alg": "HS256", "typ": "JWT"})
  - payload = base64url(claims)
  - signature = HMAC-SHA256(secret, header + "." + payload)

@note 跨进程 fan-out 仍受 PG 限制 (W33b 已知限制); 鉴权与持久化解耦
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config + Errors
# ---------------------------------------------------------------------------


@dataclass
class JWTConfig:
    """
    @brief JWT 鉴权配置

    @param secret          HS256 共享密钥 (从 CLI / YAML 注入, 或 ${VAR} 引用)
    @param algorithm       算法 (默认 HS256; 本类仅实现 HS256)
    @param expires_seconds token 有效期 (默认 1 小时)
    @param issuer          iss 字段 (默认 agent-swarm)
    """

    secret: str
    algorithm: str = "HS256"
    expires_seconds: int = 3600
    issuer: str = "agent-swarm"


class JWTError(Exception):
    """JWT 解析 / 验证失败 (签名错 / 过期 / 格式错)"""


# ---------------------------------------------------------------------------
# JWTIssuer — 编码 / 解码
# ---------------------------------------------------------------------------


def _b64url_encode(data: bytes) -> str:
    """base64url 编码 (无 padding)"""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    """base64url 解码 (补 padding)"""
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


class JWTIssuer:
    """
    @brief JWT HS256 签发 + 验证

    用法:
        issuer = JWTIssuer(JWTConfig(secret="..."))
        token = issuer.encode("user-1", {"role": "admin"})
        claims = issuer.decode(token)  # {"sub": "user-1", "role": "admin", "exp": ..., "iat": ..., "iss": ...}
    """

    def __init__(self, config: JWTConfig) -> None:
        if not config.secret:
            raise ValueError("JWTConfig.secret is required")
        if config.algorithm != "HS256":
            raise ValueError(f"only HS256 supported, got {config.algorithm!r}")
        self.config = config

    def encode(self, subject: str, claims: dict[str, Any] | None = None) -> str:
        """
        @brief 签发 token

        @param subject  sub 字段 (用户/agent id)
        @param claims   自定义 claim (role / tenant / 等)
        @return 紧凑序列化 token (header.payload.signature)
        """
        now = int(time.time())
        payload: dict[str, Any] = {
            "sub": subject,
            "iat": now,
            "exp": now + self.config.expires_seconds,
            "iss": self.config.issuer,
        }
        if claims:
            payload.update(claims)
        header = {"alg": "HS256", "typ": "JWT"}
        h_b64 = _b64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
        p_b64 = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        signing_input = f"{h_b64}.{p_b64}".encode("ascii")
        sig = hmac.new(
            self.config.secret.encode("utf-8"),
            signing_input,
            hashlib.sha256,
        ).digest()
        s_b64 = _b64url_encode(sig)
        return f"{h_b64}.{p_b64}.{s_b64}"

    def decode(self, token: str) -> dict[str, Any]:
        """
        @brief 解析 + 验证 token

        @return claims dict
        @raise JWTError 格式错 / 签名错 / 过期
        """
        if not token:
            raise JWTError("empty token")
        parts = token.split(".")
        if len(parts) != 3:
            raise JWTError(f"token must have 3 parts, got {len(parts)}")
        h_b64, p_b64, s_b64 = parts
        # 验签 (constant-time compare 防时序攻击)
        signing_input = f"{h_b64}.{p_b64}".encode("ascii")
        expected_sig = hmac.new(
            self.config.secret.encode("utf-8"),
            signing_input,
            hashlib.sha256,
        ).digest()
        try:
            actual_sig = _b64url_decode(s_b64)
        except Exception as exc:
            raise JWTError(f"signature decode failed: {exc}") from exc
        if not hmac.compare_digest(expected_sig, actual_sig):
            raise JWTError("signature mismatch")
        # 解 header + payload
        try:
            header = json.loads(_b64url_decode(h_b64).decode("utf-8"))
            payload = json.loads(_b64url_decode(p_b64).decode("utf-8"))
        except Exception as exc:
            raise JWTError(f"header/payload decode failed: {exc}") from exc
        if header.get("alg") != "HS256":
            raise JWTError(f"unsupported alg: {header.get('alg')!r}")
        # 验过期
        now = int(time.time())
        exp = payload.get("exp")
        if exp is None:
            raise JWTError("exp claim missing")
        if now >= int(exp):
            raise JWTError(f"token expired (now={now} exp={exp})")
        return payload


# ---------------------------------------------------------------------------
# FastAPI 集成
# ---------------------------------------------------------------------------


def get_jwt_issuer(request: Any) -> JWTIssuer | None:
    """
    @brief 从 app.state 拿 JWTIssuer (None 表示未启用鉴权)

    用于 Depends: 没有 jwt_secret 时 None (zero-break)
    """
    return getattr(request.app.state, "jwt_issuer", None)


def get_current_user(request: Any) -> dict[str, Any] | None:
    """
    @brief 取当前用户 (未鉴权时 None)

    Depends(get_current_user) — 路由可选择性使用
    """
    return getattr(request.state, "user", None)


def require_user(request: Any) -> dict[str, Any]:
    """
    @brief 强制鉴权 (未鉴权时 401)

    Depends(require_user) — 关键 API 用这个
    """
    user = get_current_user(request)
    if user is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=401, detail="unauthorized")
    return user


# ---------------------------------------------------------------------------
# ${VAR} 解析 (复用 W20 风格)
# ---------------------------------------------------------------------------


_SECRET_REF_RE_PREFIX = "${"
_SECRET_REF_RE_SUFFIX = "}"


def resolve_secret_ref(ref: str, env: dict[str, str] | None = None) -> str:
    """
    @brief 解析 ${VAR} 引用 → 从 env 取值

    @param ref  形如 "${WEB_JWT_SECRET}" 或 "literal-value"
    @param env  环境变量字典 (默认用 os.environ)
    @return 解析后的明文值
    """
    import os
    if not (ref.startswith(_SECRET_REF_RE_PREFIX) and ref.endswith(_SECRET_REF_RE_SUFFIX)):
        return ref
    var_name = ref[2:-1]
    src = env if env is not None else dict(os.environ)
    if var_name not in src:
        raise ValueError(f"env var {var_name!r} not set (referenced by {ref!r})")
    return src[var_name]


__all__ = [
    "JWTConfig",
    "JWTError",
    "JWTIssuer",
    "get_jwt_issuer",
    "get_current_user",
    "require_user",
    "resolve_secret_ref",
]
