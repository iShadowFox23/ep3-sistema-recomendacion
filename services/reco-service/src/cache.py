"""Caché de respuestas (TTL 15 min): Redis/ElastiCache si hay REDIS_URL, si no, memoria local."""
from __future__ import annotations

import json
import time
from collections import OrderedDict
from typing import Protocol


class Cache(Protocol):
    def get(self, key: str) -> dict | None: ...
    def set(self, key: str, value: dict) -> None: ...


class MemoryCache:
    """LRU con expiración; suficiente para una réplica (cada réplica tiene su propia copia)."""

    def __init__(self, ttl_s: int = 900, max_items: int = 10_000):
        self.ttl_s, self.max_items = ttl_s, max_items
        self._data: OrderedDict[str, tuple[float, dict]] = OrderedDict()

    def get(self, key: str) -> dict | None:
        entry = self._data.get(key)
        if entry is None:
            return None
        expires, value = entry
        if expires < time.monotonic():
            del self._data[key]
            return None
        self._data.move_to_end(key)
        return value

    def set(self, key: str, value: dict) -> None:
        self._data[key] = (time.monotonic() + self.ttl_s, value)
        self._data.move_to_end(key)
        while len(self._data) > self.max_items:
            self._data.popitem(last=False)


class RedisCache:
    """Compartida entre réplicas. Si Redis falla, se comporta como un miss (no rompe la respuesta)."""

    def __init__(self, url: str, ttl_s: int = 900):
        import redis

        self.ttl_s = ttl_s
        self._redis = redis.Redis.from_url(url, socket_timeout=0.05, socket_connect_timeout=0.2)

    def get(self, key: str) -> dict | None:
        try:
            raw = self._redis.get(key)
            return json.loads(raw) if raw else None
        except Exception:
            return None

    def set(self, key: str, value: dict) -> None:
        try:
            self._redis.setex(key, self.ttl_s, json.dumps(value))
        except Exception:
            pass


def build_cache(redis_url: str | None, ttl_s: int) -> Cache:
    return RedisCache(redis_url, ttl_s) if redis_url else MemoryCache(ttl_s)
