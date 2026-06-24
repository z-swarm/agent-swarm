"""
@module tools.verify_w36a_dod
@brief  P5-W36a DoD 守门脚本——8 项检查

P5-W36a Plan §5 Check 守门点:
  1. SecretRef 协议 (literal / ${VAR} / secret:// 三种)
  2. parse_secret_ref 错误路径 (空串 / 空 VAR / 空 key)
  3. JWTConfig 互斥校验 (W34 字面值 + W36a ref 互不兼容)
  4. create_app 接受 jwt_secret_ref + secret_manager (W36a 模式)
  5. EnvSecretManager 集成 (W36a 默认)
  6. resolve_secret 失败时降级 (cache 命中 → 继续用, miss → JWTError)
  7. version 变化 → cache 失效 → 重读
  8. CLI 选项 --web-jwt-secret-ref / --web-secret-manager / --vault-*

用法:
  .venv/bin/python tools/verify_w36a_dod.py
  exit 0 = 全部通过; 退出码 != 0 = 失败项
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# 让 tools/ 可导入 src/agent_swarm
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


def _check(name: str, ok: bool, detail: str = "") -> bool:
    """打印一项检查结果, 返 ok"""
    status = "PASS" if ok else "FAIL"
    line = f"[{status}] {name}"
    if detail:
        line += f" — {detail}"
    print(line)
    return ok


async def main() -> int:
    from agent_swarm.security.secret_manager import (
        Secret,
        SecretManager,
        SecretMetadata,
    )
    from agent_swarm.web import WebState, create_app
    from agent_swarm.web.auth import (
        JWTConfig,
        JWTError,
        JWTIssuer,
        parse_secret_ref,
    )

    results: list[bool] = []

    # -----------------------------------------------------------------------
    # 1. SecretRef 协议: 三种格式识别
    # -----------------------------------------------------------------------
    try:
        r1 = parse_secret_ref("literal-value")
        r2 = parse_secret_ref("${ENV_VAR}")
        r3 = parse_secret_ref("secret://web/jwt")
        ok = (
            r1.kind == "literal" and r1.value == "literal-value"
            and r2.kind == "env" and r2.value == "ENV_VAR"
            and r3.kind == "secret_ref" and r3.value == "web/jwt"
        )
        results.append(_check("1. SecretRef 协议 (literal / ${VAR} / secret://)", ok))
    except Exception as exc:
        results.append(_check("1. SecretRef 协议", False, str(exc)))

    # -----------------------------------------------------------------------
    # 2. parse_secret_ref 错误路径
    # -----------------------------------------------------------------------
    try:
        ok = True
        try:
            parse_secret_ref("")
            ok = False
        except ValueError:
            pass
        try:
            parse_secret_ref("${}")
            ok = False
        except ValueError:
            pass
        try:
            parse_secret_ref("secret://")
            ok = False
        except ValueError:
            pass
        results.append(_check("2. parse_secret_ref 错误路径 (空串/空 VAR/空 key)", ok))
    except Exception as exc:
        results.append(_check("2. parse_secret_ref 错误路径", False, str(exc)))

    # -----------------------------------------------------------------------
    # 3. JWTConfig 互斥校验
    # -----------------------------------------------------------------------
    try:
        ok = True
        # W34 模式 OK
        JWTConfig(secret="x")
        # W36a 模式 OK
        JWTConfig(secret_ref="secret://k", secret_manager=object())  # type: ignore[arg-type]
        # 互斥 FAIL
        try:
            JWTConfig(secret="x", secret_ref="secret://k", secret_manager=object())  # type: ignore[arg-type]
            ok = False
        except ValueError:
            pass
        # 都缺 FAIL
        try:
            JWTConfig()
            ok = False
        except ValueError:
            pass
        results.append(_check("3. JWTConfig 互斥校验", ok))
    except Exception as exc:
        results.append(_check("3. JWTConfig 互斥校验", False, str(exc)))

    # -----------------------------------------------------------------------
    # 4. create_app W36a 模式 (literal ref)
    # -----------------------------------------------------------------------
    try:
        app = create_app(web_state=WebState(), jwt_secret_ref="my-literal")
        iss = app.state.jwt_issuer
        ok = iss is not None and iss.config.secret == "my-literal"
        results.append(_check("4. create_app W36a literal ref 模式", ok))
    except Exception as exc:
        results.append(_check("4. create_app W36a literal ref 模式", False, str(exc)))

    # -----------------------------------------------------------------------
    # 5. EnvSecretManager 集成 (默认)
    # -----------------------------------------------------------------------
    try:
        import os
        os.environ["W36A_VERIFY_KEY"] = "verify-test-secret"
        try:
            app = create_app(
                web_state=WebState(),
                jwt_secret_ref="secret://W36A_VERIFY_KEY",
            )
            iss = app.state.jwt_issuer
            ok = iss is not None and iss._ref is not None and iss._ref.kind == "secret_ref"
            results.append(_check("5. create_app secret:// 默认 EnvSecretManager", ok))
        finally:
            del os.environ["W36A_VERIFY_KEY"]
    except Exception as exc:
        results.append(_check("5. EnvSecretManager 集成", False, str(exc)))

    # -----------------------------------------------------------------------
    # 6. resolve_secret 失败降级: cache miss → JWTError
    # -----------------------------------------------------------------------
    try:
        class _FailMgr(SecretManager):
            async def get(self, key: str) -> Secret:
                raise RuntimeError("simulated vault down")
            async def put(self, key: str, value: str, ttl_seconds: int | None = None) -> None:
                pass
            async def delete(self, key: str) -> None:
                pass
            async def rotate(self, key: str, new_value: str) -> Secret:
                raise NotImplementedError
            async def check_rotation_due(self) -> list[SecretMetadata]:
                return []
            async def close(self) -> None:
                return None

        iss = JWTIssuer(JWTConfig(
            secret_ref="secret://web/jwt", secret_manager=_FailMgr(),
        ))
        try:
            await iss.resolve_secret()
            ok = False
        except JWTError as exc:
            ok = "no cache" in str(exc)
        results.append(_check("6. resolve_secret 失败 cache miss → JWTError", ok))
    except Exception as exc:
        results.append(_check("6. resolve_secret 失败降级", False, str(exc)))

    # -----------------------------------------------------------------------
    # 7. version 变化 → cache 失效 → 重读
    # -----------------------------------------------------------------------
    try:
        class _VersionedMgr(SecretManager):
            def __init__(self) -> None:
                self._v = 0
                self._store: dict[str, Secret] = {}

            async def get(self, key: str) -> Secret:
                return self._store[key]

            async def put(self, key: str, value: str, ttl_seconds: int | None = None) -> None:
                self._v += 1
                self._store[key] = Secret(
                    value=value,
                    metadata=SecretMetadata(key=key, version=self._v),
                )

            async def delete(self, key: str) -> None:
                self._store.pop(key, None)

            async def rotate(self, key: str, new_value: str) -> Secret:
                await self.put(key, new_value)
                return await self.get(key)

            async def check_rotation_due(self) -> list[SecretMetadata]:
                return []

            async def close(self) -> None:
                return None

        mgr = _VersionedMgr()
        await mgr.put("k", "v1")
        iss = JWTIssuer(JWTConfig(secret_ref="secret://k", secret_manager=mgr))
        await iss.resolve_secret()
        v1 = iss._cached_version
        await mgr.rotate("k", "v2")
        await iss.resolve_secret()
        v2 = iss._cached_version
        ok = v1 == 1 and v2 == 2
        results.append(_check("7. version 变化 → cache 失效", ok))
    except Exception as exc:
        results.append(_check("7. version 变化", False, str(exc)))

    # -----------------------------------------------------------------------
    # 8. CLI 选项存在
    # -----------------------------------------------------------------------
    try:
        from click.testing import CliRunner

        from agent_swarm.cli.main import cli

        runner = CliRunner()
        res = runner.invoke(cli, ["run", "--help"])
        ok = (
            res.exit_code == 0
            and "--web-jwt-secret-ref" in res.stdout
            and "--web-secret-manager" in res.stdout
            and "--vault-url" in res.stdout
            and "--vault-role-id" in res.stdout
            and "--vault-secret-id" in res.stdout
        )
        results.append(_check("8. CLI --web-jwt-secret-ref / --vault-* 选项", ok))
    except Exception as exc:
        results.append(_check("8. CLI 选项", False, str(exc)))

    # -----------------------------------------------------------------------
    # 汇总
    # -----------------------------------------------------------------------
    print()
    passed = sum(results)
    total = len(results)
    print(f"=== W36a DoD: {passed}/{total} PASSED ===")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
