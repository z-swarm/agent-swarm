"""单元测试：KnowledgeBase——文档 / 代码搜索 / 缓存 / 命中率"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agent_swarm.core.knowledge_base import (
    KnowledgeBase,
    KnowledgeBaseRegistry,
    _estimate_size,
)

# ---------------------------------------------------------------------------
# 项目文档
# ---------------------------------------------------------------------------


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "README.md").write_text("# project\nhello\n", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("agent guide", encoding="utf-8")
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "intro.md").write_text("intro page", encoding="utf-8")
    (tmp_path / "ignore.bin").write_bytes(b"\x00\x01\x02")
    return tmp_path


async def test_get_project_docs_default_patterns(workspace: Path) -> None:
    kb = KnowledgeBase(workspace=workspace)
    docs = await kb.get_project_docs()
    paths = {d.path for d in docs}
    assert "README.md" in paths
    assert "AGENTS.md" in paths
    # docs/**/*.md 应匹配到子目录文档
    assert any("intro" in p for p in paths)


async def test_get_project_docs_custom_patterns(workspace: Path) -> None:
    kb = KnowledgeBase(workspace=workspace)
    docs = await kb.get_project_docs(patterns=["AGENTS.md"])
    assert len(docs) == 1
    assert docs[0].path == "AGENTS.md"
    assert docs[0].content == "agent guide"


async def test_get_project_docs_handles_unreadable(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """读取异常应被吞，不影响其他文档"""
    kb = KnowledgeBase(workspace=workspace)

    real_read = Path.read_text

    def boom(self, *args, **kwargs):
        if self.name == "README.md":
            raise OSError("permission denied")
        return real_read(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", boom)
    docs = await kb.get_project_docs()
    paths = {d.path for d in docs}
    assert "README.md" not in paths
    assert "AGENTS.md" in paths  # 其他仍读取成功


# ---------------------------------------------------------------------------
# 代码搜索
# ---------------------------------------------------------------------------


@pytest.fixture
def code_workspace(tmp_path: Path) -> Path:
    (tmp_path / "main.py").write_text(
        "def login():\n    SELECT_USER = 'SELECT * FROM users'\n"
        "    return query(SELECT_USER)\n",
        encoding="utf-8",
    )
    (tmp_path / "auth.py").write_text(
        "import sqlite3\nq = 'SELECT id FROM accounts'\n",
        encoding="utf-8",
    )
    (tmp_path / "README.md").write_text("docs", encoding="utf-8")
    return tmp_path


async def test_search_code_finds_matches(code_workspace: Path) -> None:
    kb = KnowledgeBase(workspace=code_workspace)
    results = await kb.search_code("SELECT")
    assert len(results) >= 2
    # 包含 file_path / 行号
    paths = {r.file_path for r in results}
    assert "main.py" in paths
    assert "auth.py" in paths


async def test_search_code_empty_query_returns_empty(code_workspace: Path) -> None:
    kb = KnowledgeBase(workspace=code_workspace)
    assert await kb.search_code("") == []


async def test_search_code_filters_by_pattern(code_workspace: Path) -> None:
    """file_pattern 过滤——只搜 *.py，不搜 README"""
    kb = KnowledgeBase(workspace=code_workspace)
    results = await kb.search_code("docs", file_pattern="**/*.py")
    assert results == []
    results2 = await kb.search_code("docs", file_pattern="**/*.md")
    assert len(results2) == 1


async def test_search_code_max_results_limit(code_workspace: Path) -> None:
    """max_results 限制返回数量"""
    kb = KnowledgeBase(workspace=code_workspace)
    results = await kb.search_code("SELECT", max_results=1)
    assert len(results) == 1


async def test_search_code_includes_context_lines(code_workspace: Path) -> None:
    """命中行的上下文应被包含"""
    kb = KnowledgeBase(workspace=code_workspace)
    results = await kb.search_code("SELECT_USER", file_pattern="**/*.py")
    assert len(results) > 0
    # 命中那条 + 前后 context
    assert "def login" in results[0].content or "return query" in results[0].content


async def test_search_code_language_detected(code_workspace: Path) -> None:
    kb = KnowledgeBase(workspace=code_workspace)
    py_results = await kb.search_code("SELECT", file_pattern="**/*.py")
    assert all(r.language == "python" for r in py_results)


# ---------------------------------------------------------------------------
# 缓存——基础读写
# ---------------------------------------------------------------------------


async def test_cache_set_then_get() -> None:
    kb = KnowledgeBase()
    await kb.cache_analysis("k1", {"finding": "sql injection"})
    val = await kb.get_cached_analysis("k1")
    assert val == {"finding": "sql injection"}


async def test_cache_miss_returns_none() -> None:
    kb = KnowledgeBase()
    assert await kb.get_cached_analysis("nope") is None


async def test_cache_overwrite_same_key() -> None:
    kb = KnowledgeBase()
    await kb.cache_analysis("k", "v1")
    await kb.cache_analysis("k", "v2")
    assert await kb.get_cached_analysis("k") == "v2"


async def test_cache_ttl_expiry() -> None:
    kb = KnowledgeBase()
    # 立即过期
    await kb.cache_analysis("k", "v", ttl=0.0)
    # ttl=0 → expires_at 立即；time.time() 已经 > expires
    val = await kb.get_cached_analysis("k")
    assert val is None


async def test_cache_ttl_not_yet_expired() -> None:
    kb = KnowledgeBase()
    await kb.cache_analysis("k", "v", ttl=10.0)
    val = await kb.get_cached_analysis("k")
    assert val == "v"


async def test_cache_no_ttl_persists() -> None:
    kb = KnowledgeBase()
    await kb.cache_analysis("k", "v")  # ttl=None
    # 过一会儿应仍能拿到
    await asyncio.sleep(0.01)
    assert await kb.get_cached_analysis("k") == "v"


# ---------------------------------------------------------------------------
# 缓存——LRU 淘汰
# ---------------------------------------------------------------------------


async def test_cache_lru_evicts_oldest() -> None:
    """size cap 触发后 LRU 淘汰最旧项"""
    # 把 cap 设很小，强制淘汰
    kb = KnowledgeBase(max_cache_size_bytes=200)
    # 每个 100 字节字符串
    big = "x" * 100
    await kb.cache_analysis("k1", big)
    await kb.cache_analysis("k2", big)
    # k3 写入会淘汰 k1（最旧）
    await kb.cache_analysis("k3", big)

    assert await kb.get_cached_analysis("k1") is None  # 被淘汰
    assert await kb.get_cached_analysis("k2") is not None
    assert await kb.get_cached_analysis("k3") is not None


async def test_cache_lru_promotes_on_hit() -> None:
    """命中应将该项移到 MRU——避免被淘汰"""
    kb = KnowledgeBase(max_cache_size_bytes=200)
    big = "x" * 100
    await kb.cache_analysis("k1", big)
    await kb.cache_analysis("k2", big)
    # 命中 k1 → k1 升到 MRU
    await kb.get_cached_analysis("k1")
    # 写入 k3 → k2 被淘汰（不是 k1）
    await kb.cache_analysis("k3", big)
    assert await kb.get_cached_analysis("k1") is not None
    assert await kb.get_cached_analysis("k2") is None


# ---------------------------------------------------------------------------
# 命中率统计——W4 DoD 关键路径
# ---------------------------------------------------------------------------


async def test_stats_tracks_hits_and_misses() -> None:
    kb = KnowledgeBase()
    await kb.cache_analysis("k", "v")
    await kb.get_cached_analysis("k")    # hit
    await kb.get_cached_analysis("k")    # hit
    await kb.get_cached_analysis("nope") # miss

    stats = await kb.stats()
    assert stats["hits"] == 2
    assert stats["misses"] == 1
    assert stats["hit_rate"] == pytest.approx(2 / 3)


async def test_stats_hit_rate_zero_when_no_lookups() -> None:
    kb = KnowledgeBase()
    stats = await kb.stats()
    assert stats["hit_rate"] == 0.0


async def test_clear_resets_state() -> None:
    kb = KnowledgeBase()
    await kb.cache_analysis("k", "v")
    await kb.get_cached_analysis("k")
    await kb.clear()

    stats = await kb.stats()
    assert stats["entries"] == 0
    assert stats["hits"] == 0
    assert stats["misses"] == 0
    assert await kb.get_cached_analysis("k") is None


# ---------------------------------------------------------------------------
# 并发安全
# ---------------------------------------------------------------------------


async def test_concurrent_cache_writes_are_safe() -> None:
    kb = KnowledgeBase()

    async def writer(i: int) -> None:
        await kb.cache_analysis(f"k{i}", f"v{i}")

    await asyncio.gather(*[writer(i) for i in range(20)])
    stats = await kb.stats()
    assert stats["entries"] == 20


async def test_concurrent_cache_reads_increment_hits() -> None:
    kb = KnowledgeBase()
    await kb.cache_analysis("k", "v")

    async def reader() -> None:
        await kb.get_cached_analysis("k")

    await asyncio.gather(*[reader() for _ in range(10)])
    stats = await kb.stats()
    assert stats["hits"] == 10


# ---------------------------------------------------------------------------
# Registry（per-tenant）
# ---------------------------------------------------------------------------


async def test_registry_returns_same_instance_per_tenant(tmp_path: Path) -> None:
    reg = KnowledgeBaseRegistry()
    kb1 = await reg.get_or_create("tenantA", workspace=tmp_path)
    kb2 = await reg.get_or_create("tenantA", workspace=tmp_path)
    assert kb1 is kb2


async def test_registry_separates_tenants(tmp_path: Path) -> None:
    reg = KnowledgeBaseRegistry()
    kb_a = await reg.get_or_create("A", workspace=tmp_path)
    kb_b = await reg.get_or_create("B", workspace=tmp_path)
    assert kb_a is not kb_b

    # 隔离：A 的缓存 B 看不到
    await kb_a.cache_analysis("k", "secret_A")
    assert await kb_b.get_cached_analysis("k") is None


async def test_registry_all_stats(tmp_path: Path) -> None:
    reg = KnowledgeBaseRegistry()
    kb_a = await reg.get_or_create("A", workspace=tmp_path)
    kb_b = await reg.get_or_create("B", workspace=tmp_path)
    await kb_a.cache_analysis("k", "v")
    await kb_b.cache_analysis("k", "v")
    stats = await reg.all_stats()
    assert set(stats.keys()) == {"A", "B"}
    assert stats["A"]["entries"] == 1


# ---------------------------------------------------------------------------
# 内部 _estimate_size
# ---------------------------------------------------------------------------


def test_estimate_size_str() -> None:
    assert _estimate_size("hello") == 5


def test_estimate_size_unicode() -> None:
    """中文每字符 utf-8 编码约 3 字节"""
    assert _estimate_size("中文") == 6


def test_estimate_size_dict() -> None:
    """dict 字节数大致 = key+value 字节数 + overhead"""
    s = _estimate_size({"a": "b"})
    assert s > 0


def test_estimate_size_list() -> None:
    s = _estimate_size(["a", "b", "c"])
    assert s >= 3


def test_estimate_size_handles_circular_reference() -> None:
    """W4-Z3 回归：循环引用不应导致无限递归"""
    a: dict = {}
    a["self"] = a
    # 不应抛 RecursionError
    s = _estimate_size(a)
    assert s > 0


def test_estimate_size_handles_circular_list() -> None:
    """W4-Z3 回归：list 自引用"""
    lst: list = []
    lst.append(lst)
    s = _estimate_size(lst)
    assert s > 0
