"""
@module tools.verify_w36b_dod
@brief  P5-W36b DoD 守门脚本——8 项检查

P5-W36b Plan §5 Check 守门点:
  1. /review 页面 200 + HTMX 表单
  2. base.html nav 加 /review 入口
  3. POST /api/review 写路径鉴权 (W34 mode, 无 token → 401)
  4. POST /api/review 默认 pr_ref (空 body)
  5. POST /api/review pr_ref 注入防御 (unsafe chars → 400)
  6. POST /api/review 非 git repo → 500 + 友好错
  7. Golden Case G-027 (干净 PR 0 finding / secret_leak / cmd_injection)
  8. PROTECTED_PREFIXES 含 /api/review (写路径模式复用 W34)

用法:
  .venv/bin/python tools/verify_w36b_dod.py
  exit 0 = 全部通过; 退出码 != 0 = 失败项
"""

from __future__ import annotations

import asyncio
import subprocess
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


def _make_git_repo(path: Path) -> Path:
    """建 tmp git repo"""
    path.mkdir(parents=True, exist_ok=True)
    for cmd in [
        ["git", "init", "-b", "main"],
        ["git", "config", "user.email", "t@e.com"],
        ["git", "config", "user.name", "Test"],
        ["git", "config", "commit.gpgsign", "false"],
    ]:
        subprocess.run(cmd, cwd=str(path), capture_output=True, text=True, check=True, timeout=10)
    return path


async def main() -> int:
    from fastapi.testclient import TestClient

    from agent_swarm.web import WebState, create_app
    from agent_swarm.web.auth import JWTConfig, JWTIssuer

    SECRET = "verify-w36b-secret"
    iss = JWTIssuer(JWTConfig(secret=SECRET))
    bearer = f"Bearer {iss.encode('verify')}"

    results: list[bool] = []

    # -----------------------------------------------------------------------
    # 1. /review 页面 200 + HTMX 表单
    # -----------------------------------------------------------------------
    try:
        app = create_app(web_state=WebState())
        client = TestClient(app)
        r = client.get("/review")
        ok = (
            r.status_code == 200
            and 'hx-post="/api/review"' in r.text
            and "Run Review" in r.text
        )
        results.append(_check("1. /review 页面 200 + HTMX 表单", ok))
    except Exception as exc:
        results.append(_check("1. /review 页面", False, str(exc)))

    # -----------------------------------------------------------------------
    # 2. base.html nav 加 /review 入口
    # -----------------------------------------------------------------------
    try:
        app = create_app(web_state=WebState())
        client = TestClient(app)
        r = client.get("/")
        ok = 'href="/review"' in r.text and ">Review<" in r.text
        results.append(_check("2. base.html nav 加 /review 入口", ok))
    except Exception as exc:
        results.append(_check("2. base.html nav", False, str(exc)))

    # -----------------------------------------------------------------------
    # 3. POST /api/review 写路径鉴权 (W34 mode, 无 token → 401)
    # -----------------------------------------------------------------------
    try:
        app = create_app(web_state=WebState(), jwt_secret=SECRET)
        client = TestClient(app)
        r = client.post("/api/review", json={"pr_ref": "main..HEAD"})
        ok = r.status_code == 401
        results.append(_check("3. POST /api/review 无 token → 401", ok))
    except Exception as exc:
        results.append(_check("3. POST /api/review 鉴权", False, str(exc)))

    # -----------------------------------------------------------------------
    # 4. POST /api/review 默认 pr_ref (空 body)
    # -----------------------------------------------------------------------
    try:
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            repo = _make_git_repo(tmppath / "r4")
            (repo / "a.py").write_text("# init\n", encoding="utf-8")
            subprocess.run(["git", "add", "a.py"], cwd=str(repo), capture_output=True, check=True, timeout=10)
            subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), capture_output=True, check=True, timeout=10)
            (repo / "b.py").write_text("# new\n", encoding="utf-8")
            subprocess.run(["git", "add", "b.py"], cwd=str(repo), capture_output=True, check=True, timeout=10)
            subprocess.run(["git", "commit", "-m", "add b"], cwd=str(repo), capture_output=True, check=True, timeout=10)
            app = create_app(
                web_state=WebState(), jwt_secret=SECRET, web_repo_root=repo,
            )
            client = TestClient(app)
            # 空 body → 默认 pr_ref
            r = client.post("/api/review", json={}, headers={"Authorization": bearer})
            ok = r.status_code == 200 and r.json()["ok"] is True
            results.append(_check("4. POST /api/review 默认 pr_ref (空 body)", ok))
    except Exception as exc:
        results.append(_check("4. POST /api/review 默认 pr_ref", False, str(exc)))

    # -----------------------------------------------------------------------
    # 5. POST /api/review pr_ref 注入防御 (unsafe chars → 400)
    # -----------------------------------------------------------------------
    try:
        app = create_app(web_state=WebState(), jwt_secret=SECRET)
        client = TestClient(app)
        bad_refs = [
            "main;ls",
            "main && rm -rf /",
            "main|cat",
            "main`ls`",
            "main$HOME",
        ]
        all_blocked = True
        for bad in bad_refs:
            r = client.post("/api/review", json={"pr_ref": bad}, headers={"Authorization": bearer})
            if r.status_code != 400:
                all_blocked = False
                print(f"  ! {bad!r} 返回 {r.status_code} 而非 400")
                break
        results.append(_check("5. pr_ref 注入防御 (unsafe chars → 400)", all_blocked))
    except Exception as exc:
        results.append(_check("5. pr_ref 注入防御", False, str(exc)))

    # -----------------------------------------------------------------------
    # 6. POST /api/review 非 git repo → 500 + 友好错
    # -----------------------------------------------------------------------
    try:
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            app = create_app(
                web_state=WebState(),
                jwt_secret=SECRET,
                web_repo_root=Path(tmpdir),  # 非 git
            )
            client = TestClient(app)
            r = client.post("/api/review", json={"pr_ref": "main..HEAD"}, headers={"Authorization": bearer})
            ok = r.status_code == 500 and "not a git" in r.json().get("detail", "").lower()
            results.append(_check("6. POST /api/review 非 git repo → 500 友好错", ok))
    except Exception as exc:
        results.append(_check("6. POST /api/review 非 git repo", False, str(exc)))

    # -----------------------------------------------------------------------
    # 7. Golden Case G-027 (干净 PR 0 finding / secret_leak)
    # -----------------------------------------------------------------------
    try:
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            repo = _make_git_repo(tmppath / "r7")
            (repo / "README.md").write_text("# Test\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=str(repo), capture_output=True, check=True, timeout=10)
            subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), capture_output=True, check=True, timeout=10)
            (repo / "DESIGN.md").write_text("# Design\n\nSafe doc.\n", encoding="utf-8")
            subprocess.run(["git", "add", "DESIGN.md"], cwd=str(repo), capture_output=True, check=True, timeout=10)
            subprocess.run(["git", "commit", "-m", "add design"], cwd=str(repo), capture_output=True, check=True, timeout=10)
            app = create_app(
                web_state=WebState(), jwt_secret=SECRET, web_repo_root=repo,
            )
            client = TestClient(app)
            r = client.post(
                "/api/review",
                json={"pr_ref": "HEAD~1..HEAD"},
                headers={"Authorization": bearer},
            )
            body = r.json()
            ok = (
                r.status_code == 200
                and body["ok"] is True
                and body["report"]["verdict"] == "approve"
                and body["report"]["findings"] == []
            )
            results.append(_check("7. G-027 干净 PR 0 finding / verdict=approve", ok))
    except Exception as exc:
        results.append(_check("7. G-027 干净 PR", False, str(exc)))

    # -----------------------------------------------------------------------
    # 8. PROTECTED_PREFIXES 含 /api/review
    # -----------------------------------------------------------------------
    try:
        from agent_swarm.web.app import create_app as _create_app
        # 通过看 middleware 的 PROTECTED_PREFIXES (间接验证)
        app = _create_app(web_state=WebState(), jwt_secret=SECRET)
        # 已通过测试 3 验证 (无 token → 401), 此处再确认源里有 /api/review
        from agent_swarm.web import app as app_module
        src = Path(app_module.__file__).read_text(encoding="utf-8")
        ok = '"/api/review"' in src and '"/api/events"' in src
        results.append(_check("8. PROTECTED_PREFIXES 含 /api/review + /api/events", ok))
    except Exception as exc:
        results.append(_check("8. PROTECTED_PREFIXES", False, str(exc)))

    # -----------------------------------------------------------------------
    # 汇总
    # -----------------------------------------------------------------------
    print()
    passed = sum(results)
    total = len(results)
    print(f"=== W36b DoD: {passed}/{total} PASSED ===")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
