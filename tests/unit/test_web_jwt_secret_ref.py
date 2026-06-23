"""
@module tests.unit.test_web_jwt_secret_ref
@brief  P5-W36a SecretRef 协议单测 (≥6 cases)

覆盖:
  - SecretRef dataclass: 构造合法性 / kind 校验 / value 非空
  - parse_secret_ref: literal / ${VAR} / secret:// 三种格式
  - parse_secret_ref 错误路径: 空串 / 空 VAR / 空 key
  - JWTConfig: secret 与 secret_ref 互斥 / 至少一个必填
  - JWTConfig 互斥校验 (W34 + W36a 互不兼容)
"""

from __future__ import annotations

import pytest

from agent_swarm.web.auth import (
    JWTConfig,
    SecretRef,
    parse_secret_ref,
)

# ---------------------------------------------------------------------------
# SecretRef dataclass
# ---------------------------------------------------------------------------


def test_secretref_literal_kind() -> None:
    """字面值 SecretRef 构造正确"""
    ref = SecretRef(kind="literal", value="my-secret")
    assert ref.kind == "literal"
    assert ref.value == "my-secret"


def test_secretref_env_kind() -> None:
    """env 引用 SecretRef 构造正确"""
    ref = SecretRef(kind="env", value="MY_ENV_VAR")
    assert ref.kind == "env"
    assert ref.value == "MY_ENV_VAR"


def test_secretref_secret_ref_kind() -> None:
    """secret_ref 引用 SecretRef 构造正确"""
    ref = SecretRef(kind="secret_ref", value="web/jwt-secret")
    assert ref.kind == "secret_ref"
    assert ref.value == "web/jwt-secret"


def test_secretref_invalid_kind_raises() -> None:
    """非法 kind 抛 ValueError"""
    with pytest.raises(ValueError, match="invalid SecretRef kind"):
        SecretRef(kind="bogus", value="x")  # type: ignore[arg-type]


def test_secretref_empty_value_raises() -> None:
    """value 为空抛 ValueError"""
    with pytest.raises(ValueError, match="value cannot be empty"):
        SecretRef(kind="literal", value="")


def test_secretref_is_frozen() -> None:
    """SecretRef 是 frozen dataclass (W36a 协议契约)"""
    import dataclasses
    ref = SecretRef(kind="literal", value="x")
    with pytest.raises(dataclasses.FrozenInstanceError):
        ref.value = "y"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# parse_secret_ref — 字面值
# ---------------------------------------------------------------------------


def test_parse_literal_value() -> None:
    """无前缀字面值 → literal"""
    ref = parse_secret_ref("my-plain-secret")
    assert ref.kind == "literal"
    assert ref.value == "my-plain-secret"


def test_parse_literal_with_special_chars() -> None:
    """字面值含特殊字符 (但非 ${} / secret:// 前缀)"""
    ref = parse_secret_ref("a/b:c-d_e.f")
    assert ref.kind == "literal"
    assert ref.value == "a/b:c-d_e.f"


# ---------------------------------------------------------------------------
# parse_secret_ref — ${VAR} env 引用
# ---------------------------------------------------------------------------


def test_parse_env_ref() -> None:
    """${VAR} 引用 → env"""
    ref = parse_secret_ref("${WEB_JWT_SECRET}")
    assert ref.kind == "env"
    assert ref.value == "WEB_JWT_SECRET"


def test_parse_env_ref_with_underscore() -> None:
    """env 引用值含下划线数字"""
    ref = parse_secret_ref("${MY_VAR_123}")
    assert ref.kind == "env"
    assert ref.value == "MY_VAR_123"


# ---------------------------------------------------------------------------
# parse_secret_ref — secret://key SecretManager 引用
# ---------------------------------------------------------------------------


def test_parse_secret_ref_url() -> None:
    """secret://key 引用 → secret_ref"""
    ref = parse_secret_ref("secret://web/jwt-secret")
    assert ref.kind == "secret_ref"
    assert ref.value == "web/jwt-secret"


def test_parse_secret_ref_url_simple_key() -> None:
    """secret://simple_key 简单 key"""
    ref = parse_secret_ref("secret://jwt")
    assert ref.kind == "secret_ref"
    assert ref.value == "jwt"


def test_parse_secret_ref_url_empty_key_raises() -> None:
    """secret:// 后空 key 抛 ValueError"""
    with pytest.raises(ValueError, match="empty SecretManager key"):
        parse_secret_ref("secret://")


# ---------------------------------------------------------------------------
# parse_secret_ref — 错误路径
# ---------------------------------------------------------------------------


def test_parse_empty_string_raises() -> None:
    """空字符串抛 ValueError"""
    with pytest.raises(ValueError, match="empty secret ref"):
        parse_secret_ref("")


def test_parse_env_ref_empty_var_raises() -> None:
    """${} 空 var 抛 ValueError"""
    with pytest.raises(ValueError, match="empty env var name"):
        parse_secret_ref("${}")


def test_parse_dollar_brace_partial_raises() -> None:
    """$VAR (无大括号) → 视为字面值 (宽松策略)"""
    # 设计: 缺 { 视为字面值, 与 W34 行为一致
    ref = parse_secret_ref("$VAR")
    assert ref.kind == "literal"
    assert ref.value == "$VAR"


# ---------------------------------------------------------------------------
# JWTConfig 互斥校验
# ---------------------------------------------------------------------------


def test_jwtconfig_w34_mode_works() -> None:
    """W34 模式: secret 字面值通过"""
    cfg = JWTConfig(secret="my-secret")
    assert cfg.secret == "my-secret"
    assert cfg.secret_ref is None
    assert cfg.secret_manager is None


def test_jwtconfig_w36a_mode_works() -> None:
    """W36a 模式: secret_ref + secret_manager 通过"""
    mgr = object()  # 实际是 SecretManager 实例, 此处仅校验互斥
    cfg = JWTConfig(secret_ref="secret://web/jwt", secret_manager=mgr)  # type: ignore[arg-type]
    assert cfg.secret is None
    assert cfg.secret_ref == "secret://web/jwt"
    assert cfg.secret_manager is mgr


def test_jwtconfig_both_secret_and_ref_raises() -> None:
    """secret + secret_ref 同时给出抛 ValueError"""
    mgr = object()
    with pytest.raises(ValueError, match="mutually exclusive"):
        JWTConfig(secret="x", secret_ref="secret://y", secret_manager=mgr)  # type: ignore[arg-type]


def test_jwtconfig_neither_raises() -> None:
    """secret + secret_ref 都缺省抛 ValueError"""
    with pytest.raises(ValueError, match="requires either"):
        JWTConfig()


def test_jwtconfig_secret_ref_without_manager_raises_at_init_time() -> None:
    """W36a 模式 JWTIssuer 校验: secret_ref 但无 secret_manager"""
    # JWTConfig 自身不强制 (允许先建 config), 强制在 JWTIssuer.__init__
    cfg = JWTConfig(secret_ref="secret://web/jwt")
    assert cfg.secret is None
    assert cfg.secret_ref == "secret://web/jwt"
    assert cfg.secret_manager is None
    # 实际校验在 JWTIssuer 创建时
    from agent_swarm.web.auth import JWTIssuer
    with pytest.raises(ValueError, match="secret_ref \\+ secret_manager"):
        JWTIssuer(cfg)
