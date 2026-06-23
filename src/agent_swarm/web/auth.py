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
from typing import TYPE_CHECKING, Any, Literal

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from agent_swarm.security.secret_manager import SecretManager


# ---------------------------------------------------------------------------
# Config + Errors
# ---------------------------------------------------------------------------


@dataclass
class JWTConfig:
    """
    @brief JWT 鉴权配置

    W34 模式 (向后兼容, 字面值预解析):
        JWTConfig(secret="literal-or-${RESOLVED}")
        调用方在传入前自行 resolve_secret_ref
    W36a 模式 (SecretManager 集成, 支持轮换):
        JWTConfig(secret_ref="secret://web/jwt-secret", secret_manager=EnvSecretManager())
        decode 时实时从 SecretManager 拿 secret, version 变化自动失效 cache

    @param secret          W34 兼容: 预解析的字面值
    @param secret_ref      W36a 新: 引用字符串 (literal / ${VAR} / secret://key)
    @param secret_manager  W36a 新: SecretManager 实例 (仅 secret_ref="secret://" 模式需要)
    @param algorithm       算法 (默认 HS256; 本类仅实现 HS256)
    @param expires_seconds token 有效期 (默认 1 小时)
    @param issuer          iss 字段 (默认 agent-swarm)
    """

    secret: str | None = None
    secret_ref: str | None = None
    secret_manager: SecretManager | None = None
    algorithm: str = "HS256"
    expires_seconds: int = 3600
    issuer: str = "agent-swarm"

    def __post_init__(self) -> None:
        # 校验:secret 与 secret_ref 互斥,secret_ref="secret://" 必须配 secret_manager
        if self.secret is None and self.secret_ref is None:
            raise ValueError("JWTConfig requires either secret (W34) or secret_ref (W36a)")
        if self.secret is not None and self.secret_ref is not None:
            raise ValueError("JWTConfig.secret and secret_ref are mutually exclusive")
        if self.algorithm != "HS256":
            raise ValueError(f"only HS256 supported, got {self.algorithm!r}")


# ---------------------------------------------------------------------------
# SecretRef 协议 (W36a)
# ---------------------------------------------------------------------------


SecretRefKind = Literal["literal", "env", "secret_ref"]


@dataclass(frozen=True)
class SecretRef:
    """
    @brief W36a secret 引用协议

    三种 kind:
      - "literal": 字面值 (W34 兼容, ref 字符串本身就是 secret)
      - "env": ${VAR} 引用, value 是 env var 名 (W34 兼容, 调用方 resolve)
      - "secret_ref": secret://key 引用, value 是 SecretManager key (W36a 新)

    @note value 的语义由 kind 决定
    """

    kind: SecretRefKind
    value: str

    def __post_init__(self) -> None:
        if self.kind not in ("literal", "env", "secret_ref"):
            raise ValueError(f"invalid SecretRef kind: {self.kind!r}")
        if not self.value:
            raise ValueError(f"SecretRef.value cannot be empty (kind={self.kind!r})")


def parse_secret_ref(ref: str) -> SecretRef:
    """
    @brief 解析 secret 引用字符串 → SecretRef

    支持格式:
      - "literal-value" → SecretRef(kind="literal", value=ref)
      - "${VAR}" → SecretRef(kind="env", value="VAR")
      - "secret://key" → SecretRef(kind="secret_ref", value="key")

    @param ref  引用字符串
    @return SecretRef dataclass (frozen)
    @raise ValueError ref 为空 或 secret:// 后无 key
    """
    if not ref:
        raise ValueError("empty secret ref")
    # 1) ${VAR} 模式 (W34 兼容)
    if ref.startswith("${") and ref.endswith("}"):
        var_name = ref[2:-1]
        if not var_name:
            raise ValueError(f"empty env var name in {ref!r}")
        return SecretRef(kind="env", value=var_name)
    # 2) secret://key 模式 (W36a 新)
    if ref.startswith("secret://"):
        key = ref[len("secret://"):]
        if not key:
            raise ValueError(f"empty SecretManager key in {ref!r}")
        return SecretRef(kind="secret_ref", value=key)
    # 3) 字面值
    return SecretRef(kind="literal", value=ref)


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

    用法 (W34 字面值模式, 向后兼容):
        issuer = JWTIssuer(JWTConfig(secret="literal-secret"))
        token = issuer.encode("user-1", {"role": "admin"})
        claims = issuer.decode(token)

    用法 (W36a SecretManager 模式, 支持轮换):
        mgr = EnvSecretManager()
        config = JWTConfig(secret_ref="secret://web/jwt", secret_manager=mgr)
        issuer = JWTIssuer(config)
        await issuer.resolve_secret()  # 初始化 cache
        token = issuer.encode(...)
        claims = issuer.decode(token)  # 走 cache (sync)
        # 轮换: 调 issuer.invalidate_cache() 让下次 resolve_secret 重读
    """

    def __init__(self, config: JWTConfig) -> None:
        # W34 模式: secret 必须非空
        # W36a 模式: secret_ref + secret_manager 必须给
        self._ref: SecretRef | None = None
        if config.secret is not None and config.secret == "":
            # W34 兼容: 空字符串仍视为缺省 (保持 W34 行为)
            raise ValueError("JWTConfig.secret is required (W34 mode)")
        if config.secret is None:
            if config.secret_ref is None or config.secret_manager is None:
                raise ValueError(
                    "JWTConfig requires either secret (W34) "
                    "or secret_ref + secret_manager (W36a)"
                )
            # W36a 模式: 校验 secret_ref 格式合法
            self._ref = parse_secret_ref(config.secret_ref)
            if self._ref.kind != "secret_ref":
                # 字面值 / env 模式在 W36a 走 secret_ref 字段但仍可工作
                # (字面值: 直接用; env: 一次性 resolve 进 secret 字段)
                if self._ref.kind == "env":
                    # env 模式: 走 resolve_secret_ref 一次性 resolve
                    import os
                    env_val = os.environ.get(self._ref.value)
                    if env_val is None:
                        raise ValueError(
                            f"env var {self._ref.value!r} not set "
                            f"(referenced by {config.secret_ref!r})"
                        )
                    config.secret = env_val
                else:  # literal
                    config.secret = self._ref.value
        self.config = config
        # W36a cache: (key, version) → secret_bytes
        self._cached_secret: bytes | None = None
        self._cached_version: int = -1
        self._cached_key: str | None = None

    async def resolve_secret(self) -> bytes:
        """
        @brief W36a: 从 SecretManager 解析最新 secret (走 cache)

        行为:
          - 首次调用: 走 SecretManager.get, 写入 cache
          - 后续调用: version 未变 → 返 cache; version 变 → 重读
          - SecretManager.get 失败: cache 命中 → 返 cache; miss → 抛 JWTError

        @return 解析后的 secret bytes
        @raise JWTError  cache miss + SecretManager 失败
        """
        if self.config.secret is not None:
            # W34 模式: 直接返 (无 SecretManager)
            return self.config.secret.encode("utf-8")
        assert self._ref is not None
        assert self.config.secret_manager is not None
        # 仅 secret_ref 模式需要 SecretManager
        if self._ref.kind != "secret_ref":
            # env / literal 在 __init__ 已 resolve 进 secret
            assert self.config.secret is not None
            return self.config.secret.encode("utf-8")
        # secret_ref 模式: 走 SecretManager (always-fresh 语义)
        # 性能: decode 走 cache (sync); resolve_secret 用于刷新 (lifespan 启动 / 定时)
        key = self._ref.value
        try:
            secret_obj = await self.config.secret_manager.get(key)
        except Exception as exc:
            if self._cached_secret is not None and self._cached_key == key:
                # 降级: cache 命中, 继续用 (不破)
                log.warning(
                    "JWTIssuer.resolve_secret: SecretManager.get(%r) failed, "
                    "using cached version=%d: %s",
                    key, self._cached_version, exc,
                )
                return self._cached_secret
            raise JWTError(
                f"SecretManager.get({key!r}) failed and no cache: {exc}"
            ) from exc
        secret_bytes = secret_obj.value.encode("utf-8")
        # cache 更新: version 变化时刷新
        if (
            self._cached_secret is None
            or self._cached_key != key
            or self._cached_version != secret_obj.metadata.version
        ):
            self._cached_secret = secret_bytes
            self._cached_key = key
            self._cached_version = secret_obj.metadata.version
            log.debug(
                "JWTIssuer.resolve_secret: cache updated key=%r version=%d",
                key, secret_obj.metadata.version,
            )
        return secret_bytes

    def invalidate_cache(self) -> None:
        """
        @brief W36a: 强制下次 resolve_secret 重读 (轮换时调)
        """
        self._cached_secret = None
        self._cached_version = -1
        self._cached_key = None

    def _current_secret_bytes(self) -> bytes:
        """
        @brief 内部: 取当前 secret bytes (sync, 走 cache 或 W34 字面值)

        W36a 必须先 await resolve_secret() 初始化 cache
        """
        if self.config.secret is not None:
            return self.config.secret.encode("utf-8")
        if self._cached_secret is None:
            raise JWTError(
                "JWTIssuer cache empty: call await resolve_secret() first"
            )
        return self._cached_secret

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
            self._current_secret_bytes(),
            signing_input,
            hashlib.sha256,
        ).digest()
        s_b64 = _b64url_encode(sig)
        return f"{h_b64}.{p_b64}.{s_b64}"

    def decode(self, token: str) -> dict[str, Any]:
        """
        @brief 解析 + 验证 token (走 cache, sync)

        @return claims dict
        @raise JWTError 格式错 / 签名错 / 过期 / cache 未初始化
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
            self._current_secret_bytes(),
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
    "SecretRef",
    "get_jwt_issuer",
    "get_current_user",
    "parse_secret_ref",
    "require_user",
    "resolve_secret_ref",
]
