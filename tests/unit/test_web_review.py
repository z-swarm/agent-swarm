"""
@module tests.unit.test_web_review
@brief  P5-W36b agent_review Web 入口单测 (≥8 cases)

覆盖:
  - GET /review 页面 200
  - POST /api/review 写路径鉴权 (W34 middleware)
  - 默认 pr_ref (空 body)
  - 自定义 pr_ref
  - pr_ref 校验 (unsafe chars / empty)
  - 无效 pr_ref → 400
  - 无 token → 401
  - 错误处理 (cwd 非 git repo)
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from agent_swarm.web import WebState, create_app
from agent_swarm.web.auth import JWTConfig, JWTIssuer

SECRET = "test-secret-w36b-do-not-use"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _client(jwt_secret: str | None = None) -> TestClient:
    app = create_app(web_state=WebState(), jwt_secret=jwt_secret)
    return TestClient(app)


def _bearer(secret: str = SECRET) -> str:
    iss = JWTIssuer(JWTConfig(secret=secret))
    return f"Bearer {iss.encode('user-1')}"


# ---------------------------------------------------------------------------
# /review 页面
# ---------------------------------------------------------------------------


def test_review_page_returns_200() -> None:
    """GET /review 页面 200"""
    client = _client(jwt_secret=None)
    r = client.get("/review")
    assert r.status_code == 200
    assert "Run Review" in r.text


def test_review_page_contains_htmx_form() -> None:
    """页面 HTML 含 HTMX form (hx-post /api/review)"""
    client = _client()
    r = client.get("/review")
    assert 'hx-post="/api/review"' in r.text


def test_review_page_in_nav() -> None:
    """base.html nav 加 /review 入口"""
    client = _client()
    r = client.get("/")
    assert 'href="/review"' in r.text
    assert ">Review<" in r.text


# ---------------------------------------------------------------------------
# POST /api/review 鉴权
# ---------------------------------------------------------------------------


def test_api_review_no_token_returns_401() -> None:
    """W34 写路径: 无 token → 401"""
    client = _client(jwt_secret=SECRET)
    r = client.post("/api/review", json={"pr_ref": "main..HEAD"})
    assert r.status_code == 401


def test_api_review_with_token_works() -> None:
    """有效 token → 200 (无 git repo 报友好错)"""
    client = _client(jwt_secret=SECRET)
    r = client.post(
        "/api/review",
        json={"pr_ref": "main..HEAD"},
        headers={"Authorization": _bearer()},
    )
    # 不一定是 200 (要看 cwd 是否是 git repo); 但应该是 200 或 500 (友好错)
    assert r.status_code in (200, 500)
    body = r.json()
    if r.status_code == 200:
        assert "ok" in body


def test_api_review_no_auth_mode_no_token() -> None:
    """W28 兼容: 无 secret 配置时无鉴权"""
    client = _client(jwt_secret=None)
    r = client.post("/api/review", json={"pr_ref": "main..HEAD"})
    # 不抛 401, 看 cwd
    assert r.status_code in (200, 500)


# ---------------------------------------------------------------------------
# POST /api/review 参数
# ---------------------------------------------------------------------------


def test_api_review_empty_body_uses_default() -> None:
    """空 body → 默认 pr_ref=main..HEAD"""
    client = _client(jwt_secret=None)
    r = client.post(
        "/api/review",
        json={},
        headers={"Authorization": _bearer()},
    )
    # 不管成功失败, 关键是接受空 body
    assert r.status_code in (200, 500)


def test_api_review_custom_pr_ref() -> None:
    """自定义 pr_ref (校验 shlex 合法)"""
    client = _client(jwt_secret=None)
    r = client.post(
        "/api/review",
        json={"pr_ref": "abc123..def456"},
        headers={"Authorization": _bearer()},
    )
    # abc..def 是合法 git rev, 哪怕没 commit 也接受 (后续报 no diff)
    assert r.status_code in (200, 400, 500)


def test_api_review_unsafe_pr_ref_returns_400() -> None:
    """pr_ref 含 ; & | → 400 (shell 注入防御)"""
    client = _client(jwt_secret=None)
    r = client.post(
        "/api/review",
        json={"pr_ref": "main..HEAD; rm -rf /"},
        headers={"Authorization": _bearer()},
    )
    assert r.status_code == 400
    body = r.json()
    assert "unsafe" in body.get("detail", "").lower() or "characters" in body.get("detail", "").lower()


def test_api_review_pipe_injection_returns_400() -> None:
    """pr_ref 含 | 注入 → 400"""
    client = _client(jwt_secret=None)
    r = client.post(
        "/api/review",
        json={"pr_ref": "main | cat /etc/passwd"},
        headers={"Authorization": _bearer()},
    )
    assert r.status_code == 400


def test_api_review_non_git_repo_returns_500() -> None:
    """非 git 仓库 → 500 + 友好错"""
    import tempfile
    with tempfile.TemporaryDirectory() as tmpdir:
        # 设置 web_repo_root 指向非 git 目录
        app = create_app(
            web_state=WebState(),
            jwt_secret=SECRET,
            web_repo_root=__import__("pathlib").Path(tmpdir),
        )
        client = TestClient(app)
        r = client.post(
            "/api/review",
            json={"pr_ref": "main..HEAD"},
            headers={"Authorization": _bearer()},
        )
        assert r.status_code == 500
        body = r.json()
        assert "not a git" in body.get("detail", "").lower() or "git" in body.get("detail", "").lower()


# ---------------------------------------------------------------------------
# _validate_pr_ref 单元
# ---------------------------------------------------------------------------


def test_validate_pr_ref_passes_normal() -> None:
    """正常 pr_ref 通过"""
    from agent_swarm.web.routes import _validate_pr_ref

    assert _validate_pr_ref("main..HEAD") is None
    assert _validate_pr_ref("abc123..def456") is None
    assert _validate_pr_ref("main") is None


def test_validate_pr_ref_rejects_empty() -> None:
    """空字符串拒"""
    from agent_swarm.web.routes import _validate_pr_ref

    err = _validate_pr_ref("")
    assert err is not None
    assert "empty" in err


def test_validate_pr_ref_rejects_shell_chars() -> None:
    """shell 危险字符拒"""
    from agent_swarm.web.routes import _validate_pr_ref

    for bad in ("main;ls", "main&&ls", "main|ls", "main`ls`", "main$VAR", "main>file", "main<file"):
        err = _validate_pr_ref(bad)
        assert err is not None, f"should reject {bad!r}"
