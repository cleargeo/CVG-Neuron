"""
CVG Neuron AI Orchestration System — NeuroCache
Version: 2.0.0 | Clearview Geographic LLC

Ephemeral in-process cache with LRU / LFU / FIFO eviction strategies.
Used by CognitiveProcessor for volatile short-term memory (< 512 slots).

Architecture note: NeuroCache is the "AI volatile" tier (Level 6)
in the CVG COMB tiered memory stack. For persistent storage, use
CombService which routes to PollenStore (cold) or BitHive (hot).
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Generic, List, Optional, Tuple, TypeVar

from app.core.config import settings
from app.core.logger import get_logger

log = get_logger("neuro-cache")
V = TypeVar("V")


# ── Cache Entry ───────────────────────────────────────────────────────────────

@dataclass
class CacheEntry:
    """A single cached item with metadata for eviction policy decisions."""

    key: str
    value: Any
    created_at: float = field(default_factory=time.monotonic)
    expires_at: Optional[float] = None      # absolute monotonic timestamp
    access_count: int = 0
    last_accessed: float = field(default_factory=time.monotonic)
    size_bytes: int = 0
    tags: List[str] = field(default_factory=list)
    task_id: Optional[str] = None         # Which task created this entry

    @property
    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        return time.monotonic() > self.expires_at

    @property
    def age_seconds(self) -> float:
        return time.monotonic() - self.created_at

    def touch(self) -> None:
        """Record an access."""
        self.access_count += 1
        self.last_accessed = time.monotonic()


# ── NeuroCache ────────────────────────────────────────────────────────────────

class NeuroCache:
    """
    Thread-safe in-process cache supporting LRU, LFU, and FIFO eviction.

    Designed for CVG Neuron's short-term cognitive memory:
    - Stores intermediate task results, agent responses, context windows
    - Max 512 slots by default (configurable via NEURO_CACHE_MAX_SIZE)
    - TTL-based expiry for volatile session data
    - Tagged entries for bulk invalidation by task/session

    Usage:
        cache = NeuroCache()
        await cache.set("task:abc:result", result_data, ttl=300)
        value = await cache.get("task:abc:result")
    """

    def __init__(
        self,
        max_size: int = 512,
        strategy: str = "LRU",
        default_ttl: Optional[int] = None,
    ) -> None:
        self._max_size = max_size
        self._strategy = strategy.upper()
        self._default_ttl = default_ttl or settings.cache_ttl_seconds
        self._store: OrderedDict[str, CacheEntry] = OrderedDict()
        # asyncio.Lock — never blocks the event loop unlike threading.RLock
        self._lock = asyncio.Lock()

        # Metrics
        self._hits: int = 0
        self._misses: int = 0
        self._evictions: int = 0
        self._expirations: int = 0

        log.info(
            "NeuroCache initialized",
            max_size=max_size,
            strategy=strategy,
            default_ttl=default_ttl,
        )

    # ── Public async API ──────────────────────────────────────────────────────

    async def get(self, key: str) -> Optional[Any]:
        """
        Retrieve a cached value by key.
        Returns None if key doesn't exist or has expired.
        """
        async with self._lock:
            entry = self._store.get(key)
            if entry is None:
                self._misses += 1
                return None

            if entry.is_expired:
                del self._store[key]
                self._misses += 1
                self._expirations += 1
                return None

            entry.touch()

            # LRU: move to end on access
            if self._strategy == "LRU":
                self._store.move_to_end(key)

            self._hits += 1
            return entry.value

    async def set(
        self,
        key: str,
        value: Any,
        ttl: Optional[int] = None,
        tags: Optional[List[str]] = None,
        task_id: Optional[str] = None,
    ) -> None:
        """
        Store a value in the cache.

        Args:
            key: Cache key
            value: Any serializable value
            ttl: Time-to-live in seconds (uses default if None)
            tags: Optional tags for grouped invalidation
            task_id: Task that created this entry
        """
        effective_ttl = ttl if ttl is not None else self._default_ttl
        expires_at = time.monotonic() + effective_ttl if effective_ttl > 0 else None

        entry = CacheEntry(
            key=key,
            value=value,
            expires_at=expires_at,
            tags=tags or [],
            task_id=task_id,
        )

        async with self._lock:
            # If key exists, update in place
            if key in self._store:
                self._store[key] = entry
                if self._strategy == "LRU":
                    self._store.move_to_end(key)
                return

            # Evict if at capacity
            if len(self._store) >= self._max_size:
                self._evict_one()

            if self._strategy == "LRU":
                self._store[key] = entry
                self._store.move_to_end(key)
            else:
                self._store[key] = entry

    async def delete(self, key: str) -> bool:
        """Remove a specific key. Returns True if key existed."""
        async with self._lock:
            if key in self._store:
                del self._store[key]
                return True
            return False

    async def exists(self, key: str) -> bool:
        """Check if a key exists and is not expired."""
        value = await self.get(key)
        return value is not None

    async def invalidate_by_tag(self, tag: str) -> int:
        """Remove all entries with a given tag. Returns count removed."""
        async with self._lock:
            to_delete = [k for k, e in self._store.items() if tag in e.tags]
            for key in to_delete:
                del self._store[key]
            return len(to_delete)

    async def invalidate_by_task(self, task_id: str) -> int:
        """Remove all entries created by a specific task."""
        async with self._lock:
            to_delete = [k for k, e in self._store.items() if e.task_id == task_id]
            for key in to_delete:
                del self._store[key]
            return len(to_delete)

    async def clear(self) -> None:
        """Flush the entire cache."""
        async with self._lock:
            self._store.clear()
        log.info("NeuroCache cleared")

    async def purge_expired(self) -> int:
        """Remove all expired entries. Returns count purged."""
        async with self._lock:
            expired = [k for k, e in self._store.items() if e.is_expired]
            for key in expired:
                del self._store[key]
                self._expirations += 1
        if expired:
            log.debug("NeuroCache purged expired entries", count=len(expired))
        return len(expired)

    # ── Convenience wrappers ──────────────────────────────────────────────────

    async def get_or_set(
        self,
        key: str,
        factory,
        ttl: Optional[int] = None,
        tags: Optional[List[str]] = None,
    ) -> Any:
        """
        Get cached value, or compute and cache it.

        Args:
            key: Cache key
            factory: Async callable that produces the value
            ttl: TTL in seconds
            tags: Entry tags
        """
        cached = await self.get(key)
        if cached is not None:
            return cached

        value = await factory() if asyncio.iscoroutinefunction(factory) else factory()
        await self.set(key, value, ttl=ttl, tags=tags)
        return value

    async def multi_get(self, keys: List[str]) -> Dict[str, Any]:
        """Fetch multiple keys at once. Returns dict of found key→value pairs."""
        result: Dict[str, Any] = {}
        for key in keys:
            val = await self.get(key)
            if val is not None:
                result[key] = val
        return result

    async def multi_set(
        self,
        items: Dict[str, Any],
        ttl: Optional[int] = None,
        tags: Optional[List[str]] = None,
    ) -> None:
        """Store multiple key→value pairs."""
        for key, value in items.items():
            await self.set(key, value, ttl=ttl, tags=tags)

    # ── Stats & Introspection ─────────────────────────────────────────────────

    @property
    def size(self) -> int:
        return len(self._store)

    @property
    def hit_rate(self) -> float:
        total = self._hits + self._misses
        return self._hits / total if total > 0 else 0.0

    def stats(self) -> Dict[str, Any]:
        """Return cache performance statistics."""
        return {
            "strategy": self._strategy,
            "max_size": self._max_size,
            "current_size": self.size,
            "utilization": self.size / self._max_size,
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": round(self.hit_rate, 4),
            "evictions": self._evictions,
            "expirations": self._expirations,
        }

    def keys(self) -> List[str]:
        """Return a snapshot of current cache keys (no lock — safe for GIL-protected read)."""
        return list(self._store.keys())

    # ── Internal eviction ─────────────────────────────────────────────────────

    def _evict_one(self) -> None:
        """Evict one entry based on the configured strategy."""
        if not self._store:
            return

        if self._strategy == "LRU":
            # Remove oldest (front of OrderedDict)
            key, _ = next(iter(self._store.items()))
            del self._store[key]

        elif self._strategy == "LFU":
            # Remove entry with lowest access_count
            key = min(self._store, key=lambda k: self._store[k].access_count)
            del self._store[key]

        elif self._strategy == "FIFO":
            # Remove entry with oldest created_at
            key = min(self._store, key=lambda k: self._store[k].created_at)
            del self._store[key]

        else:
            # Fallback: LRU
            key, _ = next(iter(self._store.items()))
            del self._store[key]

        self._evictions += 1


# ── Module-level singleton ────────────────────────────────────────────────────

_cache_instance: Optional[NeuroCache] = None


def get_neuro_cache() -> NeuroCache:
    """Return the global NeuroCache singleton (lazy-initialized)."""
    global _cache_instance
    if _cache_instance is None:
        _cache_instance = NeuroCache(
            max_size=settings.neuro_cache_max_size,
            strategy=settings.neuro_cache_strategy,
            default_ttl=settings.cache_ttl_seconds,
        )
    return _cache_instance
