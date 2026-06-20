"""
@module agent_swarm.core.backends
@brief  W18 TaskQueue 后端集合——memory / redis
"""

from __future__ import annotations

from agent_swarm.core.backends.memory import MemoryBackend

__all__ = ["MemoryBackend"]

# Redis 是可选依赖——按需导入
try:
    from agent_swarm.core.backends.redis_backend import (  # noqa: F401
        RedisBackend,
        RedisConfig,
    )
    __all__ += ["RedisBackend", "RedisConfig"]
except ImportError:  # pragma: no cover
    pass
