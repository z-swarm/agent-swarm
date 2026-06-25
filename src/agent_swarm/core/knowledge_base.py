"""
@module agent_swarm.core.knowledge_base
@brief  KnowledgeBase——共享知识层（W4）

DESIGN.md §6.6 完整规约：
  - per-tenant 实例（W4 单租户 "local"，W5 接入 SecurityContext 时按 tenant 分发）
  - 写多读多 → asyncio.Lock 保护
  - 三类数据:
      1) 项目文档/约定 (get_project_docs)
      2) 代码搜索结果缓存 (search_code)
      3) 共享分析缓存 (cache_analysis / get_cached_analysis)

W4 简化:
  - LRU + size cap 启用，但默认 cap = 100MB（很难触发）
  - search_code 用 grep 实现（不引外部索引；W6+ 替换为 ripgrep / tree-sitter）
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class Document:
    """项目文档（DESIGN §A.4）"""

    path: str
    content: str
    last_modified: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class CodeSnippet:
    """代码搜索结果（DESIGN §A.4）"""

    file_path: str
    line_start: int
    line_end: int
    content: str
    language: str = "text"
    score: float = 0.0


@dataclass
class _CacheEntry:
    """LRU 缓存项——内部使用"""

    value: Any
    size_bytes: int  # 估算字节数
    created_at: float
    expires_at: float | None  # None = 不过期
    hits: int = 0


class KnowledgeBase:
    """
    共享知识层——per-tenant 实例

    @note 提供 get_cached_analysis / cache_analysis 跨 agent 复用结果——
          这是 W4 KB 缓存命中率 DoD 的关键路径。
    """

    def __init__(
        self,
        workspace: Path | str | None = None,
        max_cache_size_bytes: int = 100 * 1024 * 1024,  # 100 MB
        tenant_id: str = "local",
    ) -> None:
        self.workspace = Path(workspace).resolve() if workspace else Path.cwd().resolve()
        self.tenant_id = tenant_id
        self.max_cache_size_bytes = max_cache_size_bytes

        # 缓存（OrderedDict 实现 LRU——move_to_end 提升命中项）
        self._cache: OrderedDict[str, _CacheEntry] = OrderedDict()
        self._cache_size: int = 0
        self._lock = asyncio.Lock()

        # 命中/未命中计数——用于 W4 DoD 验证
        self._hits = 0
        self._misses = 0

    # ------------------------------------------------------------------
    # 项目文档（W4 简化：直接读文件）
    # ------------------------------------------------------------------
    async def get_project_docs(self, patterns: list[str] | None = None) -> list[Document]:
        """
        读取项目文档——默认匹配 README / CLAUDE / docs/*

        @param patterns 文件模式列表（相对 workspace），默认常见文档
        """
        if patterns is None:
            patterns = [
                "README.md",
                "README.rst",
                "README.txt",
                "CLAUDE.md",
                "AGENTS.md",
                "docs/**/*.md",
            ]

        docs: list[Document] = []
        for pat in patterns:
            for p in self.workspace.glob(pat):
                if p.is_file():
                    try:
                        content = p.read_text(encoding="utf-8", errors="replace")
                    except OSError as exc:
                        log.warning("kb.read_failed path=%s err=%s", p, exc)
                        continue
                    docs.append(
                        Document(
                            path=str(p.relative_to(self.workspace)),
                            content=content,
                            last_modified=p.stat().st_mtime,
                        )
                    )
        return docs

    # ------------------------------------------------------------------
    # 代码搜索（W4 简化：朴素 grep）
    # ------------------------------------------------------------------
    async def search_code(
        self,
        query: str,
        file_pattern: str = "**/*",
        max_results: int = 50,
        context_lines: int = 2,
    ) -> list[CodeSnippet]:
        """
        在 workspace 中搜索代码——W4 朴素 grep（substring，无 regex）

        @param query 搜索字符串（大小写敏感）
        @param file_pattern glob 模式（如 "**/*.py"）
        @param max_results 最多返回项数
        @param context_lines 命中行的上下文行数
        """
        if not query:
            return []

        results: list[CodeSnippet] = []
        for p in self.workspace.glob(file_pattern):
            if not p.is_file():
                continue
            if len(results) >= max_results:
                break
            try:
                lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for i, line in enumerate(lines):
                if query in line:
                    start = max(0, i - context_lines)
                    end = min(len(lines), i + context_lines + 1)
                    snippet = "\n".join(lines[start:end])
                    results.append(
                        CodeSnippet(
                            file_path=str(p.relative_to(self.workspace)),
                            line_start=start + 1,
                            line_end=end,
                            content=snippet,
                            language=_guess_language(p),
                            score=1.0,
                        )
                    )
                    if len(results) >= max_results:
                        break
        return results

    # ------------------------------------------------------------------
    # 共享分析缓存——W4 KB 命中率 DoD 的关键
    # ------------------------------------------------------------------
    async def cache_analysis(
        self,
        key: str,
        result: Any,
        ttl: float | None = None,
    ) -> None:
        """
        缓存一次分析结果——后续 get_cached_analysis 复用

        @param key  唯一标识（约定: "skill_id:file_path:hash" 等）
        @param ttl  秒级过期；None 表示不过期
        """
        size = _estimate_size(result)
        now = time.time()
        # ttl=0.0 也算"立即过期"——用 is not None 显式判断
        expires_at = now + ttl if ttl is not None else None

        async with self._lock:
            # 已有同 key → 替换；保持 LRU 顺序
            if key in self._cache:
                self._cache_size -= self._cache[key].size_bytes
                del self._cache[key]

            entry = _CacheEntry(
                value=result,
                size_bytes=size,
                created_at=now,
                expires_at=expires_at,
            )
            self._cache[key] = entry
            self._cache_size += size

            # LRU 淘汰
            while self._cache_size > self.max_cache_size_bytes and len(self._cache) > 1:
                evicted_key, evicted_entry = self._cache.popitem(last=False)
                self._cache_size -= evicted_entry.size_bytes
                log.debug("kb.evicted key=%s size=%d", evicted_key, evicted_entry.size_bytes)

    async def get_cached_analysis(self, key: str) -> Any | None:
        """
        取缓存——命中则提升 LRU 位置

        @return 缓存值；命中失败/过期返回 None
        """
        async with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                self._misses += 1
                return None

            # 过期检查 — W42 修复: ttl=0 / 负值视为立即过期, 用 >= 保证
            # (time.time() 与 cache_analysis 里的 now 可能相等或差几微秒, 严格 > 会误判命中)
            if entry.expires_at is not None and time.time() >= entry.expires_at:
                del self._cache[key]
                self._cache_size -= entry.size_bytes
                self._misses += 1
                return None

            # 命中：移到末尾（MRU）+ 计数
            self._cache.move_to_end(key)
            entry.hits += 1
            self._hits += 1
            return entry.value

    # ------------------------------------------------------------------
    # 统计 / 调试
    # ------------------------------------------------------------------
    async def stats(self) -> dict[str, Any]:
        """缓存命中率 + 容量——W4 DoD 用此验证"""
        async with self._lock:
            total = self._hits + self._misses
            hit_rate = self._hits / total if total > 0 else 0.0
            return {
                "entries": len(self._cache),
                "size_bytes": self._cache_size,
                "max_size_bytes": self.max_cache_size_bytes,
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": hit_rate,
            }

    async def clear(self) -> None:
        """清空缓存——测试/重置用"""
        async with self._lock:
            self._cache.clear()
            self._cache_size = 0
            self._hits = 0
            self._misses = 0


# ---------------------------------------------------------------------------
# Per-tenant 注册表（DESIGN §6.6.2）
# ---------------------------------------------------------------------------


class KnowledgeBaseRegistry:
    """
    KB 实例工厂——按 tenant_id 分发

    W4 单租户：始终返回同一个 KB（tenant_id="local"）
    W5 多租户接入 SecurityContext 后按 tenant_id 区分实例
    """

    def __init__(self) -> None:
        self._instances: dict[str, KnowledgeBase] = {}
        self._lock = asyncio.Lock()

    async def get_or_create(
        self,
        tenant_id: str = "local",
        workspace: Path | str | None = None,
    ) -> KnowledgeBase:
        """获取（或创建）指定 tenant 的 KB 实例"""
        async with self._lock:
            kb = self._instances.get(tenant_id)
            if kb is None:
                kb = KnowledgeBase(workspace=workspace, tenant_id=tenant_id)
                self._instances[tenant_id] = kb
                log.debug("kb.created tenant=%s workspace=%s", tenant_id, workspace)
            return kb

    async def all_stats(self) -> dict[str, dict[str, Any]]:
        """所有 tenant 的统计——便于全局观测"""
        async with self._lock:
            tenants = list(self._instances.items())
        return {tid: await kb.stats() for tid, kb in tenants}


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------


def _estimate_size(obj: Any, _seen: set[int] | None = None) -> int:
    """
    粗估字节数——str 用 utf-8 字节数；其他用 repr 长度

    @note 防循环引用：通过 id() 跟踪已访问对象（W4-Z3 修复）
    """
    if _seen is None:
        _seen = set()
    obj_id = id(obj)
    if obj_id in _seen:
        return 0
    _seen.add(obj_id)

    if isinstance(obj, str):
        return len(obj.encode("utf-8"))
    if isinstance(obj, bytes):
        return len(obj)
    if isinstance(obj, list | tuple | set):
        return sum(_estimate_size(x, _seen) for x in obj) + 32
    if isinstance(obj, dict):
        return sum(_estimate_size(k, _seen) + _estimate_size(v, _seen) for k, v in obj.items()) + 64
    return len(repr(obj).encode("utf-8"))


_LANG_BY_EXT = {
    ".py": "python",
    ".js": "javascript",
    ".ts": "typescript",
    ".java": "java",
    ".go": "go",
    ".rs": "rust",
    ".rb": "ruby",
    ".c": "c",
    ".cpp": "cpp",
    ".h": "c",
    ".hpp": "cpp",
    ".md": "markdown",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".json": "json",
    ".sh": "bash",
    ".sql": "sql",
}


def _guess_language(path: Path) -> str:
    return _LANG_BY_EXT.get(path.suffix.lower(), "text")
