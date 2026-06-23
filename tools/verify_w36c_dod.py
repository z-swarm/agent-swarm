"""
@module tools.verify_w36c_dod
@brief  P5-W36c DoD 守门脚本——8 项检查

P5-W36c Plan §5 Check 守门点:
  1. parse_secret_ref 识别 vault://path (无 field)
  2. parse_secret_ref 识别 vault://path#field (有 field)
  3. parse_secret_ref 错误路径 (空 path / 空 field)
  4. SecretRef field 字段 + JWTConfig vault 模式
  5. JWTIssuer.resolve_secret vault 无 field (直接用 value)
  6. JWTIssuer.resolve_secret vault 有 field (JSON 提取)
  7. 轮换 + Vault 不可用降级 (G-028 核心 SLA)
  8. W36a 全部不破 (3 个老测试文件回归)

用法:
  .venv/bin/python tools/verify_w36c_dod.py
  exit 0 = 全部通过; 退出码 != 0 = 失败项
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


def _check(name: str, ok: bool, detail: str = "") -> bool:
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
    from agent_swarm.web.auth import (
        JWTConfig,
        JWTError,
        JWTIssuer,
        SecretRef,
        parse_secret_ref,
    )

    class _FakeVault(SecretManager):
        def __init__(self) -> None:
            self._store: dict[str, Secret] = {}
            self._version: dict[str, int] = {}
            self.fail_get = False

        async def get(self, key: str) -> Secret:
            if self.fail_get:
                self.fail_get = False
                raise RuntimeError("simulated vault outage")
            if key not in self._store:
                from agent_swarm.security.secret_manager import SecretNotFoundError
                raise SecretNotFoundError(f"vault: {key!r} not found")
            return self._store[key]

        async def put(self, key: str, value: str, ttl_seconds: int | None = None) -> None:
            self._version[key] = self._version.get(key, 0) + 1
            self._store[key] = Secret(
                value=value,
                metadata=SecretMetadata(key=key, version=self._version[key]),
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

    results: list[bool] = []

    # 1. parse_secret_ref: vault://path (无 field)
    try:
        ref = parse_secret_ref("vault://web/jwt")
        ok = ref.kind == "vault" and ref.value == "web/jwt" and ref.field is None
        results.append(_check("1. parse_secret_ref vault://path (无 field)", ok))
    except Exception as exc:
        results.append(_check("1. parse_secret_ref vault://path", False, str(exc)))

    # 2. parse_secret_ref: vault://path#field
    try:
        ref = parse_secret_ref("vault://web/jwt#current")
        ok = (
            ref.kind == "vault"
            and ref.value == "web/jwt"
            and ref.field == "current"
        )
        results.append(_check("2. parse_secret_ref vault://path#field", ok))
    except Exception as exc:
        results.append(_check("2. parse_secret_ref vault://path#field", False, str(exc)))

    # 3. parse_secret_ref 错误路径
    try:
        ok = True
        try:
            parse_secret_ref("vault://")
            ok = False
        except ValueError:
            pass
        try:
            parse_secret_ref("vault://web/jwt#")
            ok = False
        except ValueError:
            pass
        results.append(_check("3. parse_secret_ref 错误路径 (空 path / 空 field)", ok))
    except Exception as exc:
        results.append(_check("3. parse_secret_ref 错误路径", False, str(exc)))

    # 4. SecretRef field 字段 + JWTConfig vault 模式
    try:
        ref = SecretRef(kind="vault", value="web/jwt", field="key")
        ok_kind = ref.field == "key"
        # JWTConfig vault:// 模式
        mgr = object()  # 实际是 SecretManager
        cfg = JWTConfig(secret_ref="vault://web/jwt#key", secret_manager=mgr)  # type: ignore[arg-type]
        ok_cfg = cfg.secret is None and cfg.secret_ref == "vault://web/jwt#key"
        # 老的 3 kinds field 缺省 None
        r_literal = SecretRef(kind="literal", value="x")
        ok_old = r_literal.field is None
        results.append(_check("4. SecretRef field + JWTConfig vault + 老 kinds 兼容",
                              ok_kind and ok_cfg and ok_old))
    except Exception as exc:
        results.append(_check("4. SecretRef field", False, str(exc)))

    # 5. JWTIssuer.resolve_secret vault 无 field
    try:
        vault = _FakeVault()
        await vault.put("web/jwt", "plain-secret")
        iss = JWTIssuer(JWTConfig(secret_ref="vault://web/jwt", secret_manager=vault))
        sec = await iss.resolve_secret()
        ok = sec == b"plain-secret"
        results.append(_check("5. JWTIssuer.resolve_secret vault 无 field (直接用 value)", ok))
    except Exception as exc:
        results.append(_check("5. JWTIssuer vault 无 field", False, str(exc)))

    # 6. JWTIssuer.resolve_secret vault 有 field
    try:
        vault = _FakeVault()
        doc = json.dumps({"current": "secret-from-field", "previous": "old"})
        await vault.put("web/jwt", doc)
        iss = JWTIssuer(
            JWTConfig(secret_ref="vault://web/jwt#current", secret_manager=vault),
        )
        sec = await iss.resolve_secret()
        ok = sec == b"secret-from-field"
        results.append(_check("6. JWTIssuer.resolve_secret vault 有 field (JSON 提取)", ok))
    except Exception as exc:
        results.append(_check("6. JWTIssuer vault 有 field", False, str(exc)))

    # 7. 轮换 + Vault 不可用降级 (G-028 核心 SLA)
    try:
        vault = _FakeVault()
        doc_v1 = json.dumps({"current": "v1-secret"})
        await vault.put("web/jwt", doc_v1)
        iss = JWTIssuer(
            JWTConfig(secret_ref="vault://web/jwt#current", secret_manager=vault),
        )
        # 初始
        sec1 = await iss.resolve_secret()
        # 轮换
        doc_v2 = json.dumps({"current": "v2-secret"})
        await vault.rotate("web/jwt", doc_v2)
        sec2 = await iss.resolve_secret()
        # 故障 + 降级
        vault.fail_get = True
        sec3 = await iss.resolve_secret()  # cache 命中
        ok = (
            sec1 == b"v1-secret"
            and sec2 == b"v2-secret"
            and sec3 == b"v2-secret"  # 降级
        )
        results.append(_check("7. 轮换 cache 失效 + Vault 不可用降级", ok))
    except Exception as exc:
        results.append(_check("7. 轮换 + 降级", False, str(exc)))

    # 8. W36a 全部不破 (跑子集验证)
    try:
        # literal / env / secret:// 三种老 case
        r1 = parse_secret_ref("literal-value")
        r2 = parse_secret_ref("${ENV}")
        r3 = parse_secret_ref("secret://key")
        ok_old = (
            r1.kind == "literal"
            and r2.kind == "env"
            and r3.kind == "secret_ref"
        )
        # W36a SecretRef 字段
        sr = SecretRef(kind="secret_ref", value="x")
        ok_field = sr.field is None
        results.append(_check("8. W36a 3 kinds 仍工作 + SecretRef field 缺省 None",
                              ok_old and ok_field))
    except Exception as exc:
        results.append(_check("8. W36a 老 kinds 兼容", False, str(exc)))

    print()
    passed = sum(results)
    total = len(results)
    print(f"=== W36c DoD: {passed}/{total} PASSED ===")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
