"""Redis-backed caching, request de-duplication (single-flight locks) and rate limiting.

Every operation degrades gracefully: if Redis is unreachable the system keeps working
without caching (a process-local lock is used instead), and the failure is logged once
per minute instead of crashing callers.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Any, Iterator

import redis

log = logging.getLogger(__name__)

_RELEASE_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end
"""


class RedisCache:
    prefix = "railmon:"

    def __init__(self) -> None:
        self.client: redis.Redis | None = None
        self._local_locks: dict[str, threading.Lock] = {}
        self._local_guard = threading.Lock()
        self._last_error_log = 0.0
        self._down_until = 0.0  # circuit breaker: skip Redis briefly after a failure
        self.available = False

    # -- setup -----------------------------------------------------------------
    def init_app(self, app) -> None:
        url = app.config.get("REDIS_URL", "")
        if url.startswith("fakeredis://"):
            import fakeredis  # test dependency only

            self.client = fakeredis.FakeRedis(decode_responses=True)
        else:
            self.client = redis.Redis.from_url(
                url, decode_responses=True, socket_timeout=3, socket_connect_timeout=3, health_check_interval=30
            )
        app.extensions["railmon_cache"] = self

    @property
    def _client(self) -> redis.Redis | None:
        if self.client is None or time.time() < self._down_until:
            return None
        return self.client

    def _err(self, exc: Exception) -> None:
        self.available = False
        now = time.time()
        self._down_until = now + 30
        if now - self._last_error_log > 60:
            log.warning("Redis unavailable, continuing without cache: %s", exc)
            self._last_error_log = now

    def ping(self) -> bool:
        if self._client is None:
            self.available = False
            return False
        try:
            self.available = bool(self.client.ping())
        except redis.RedisError as exc:
            self._err(exc)
        return self.available

    # -- JSON cache ------------------------------------------------------------
    def get_json(self, key: str) -> Any | None:
        client = self._client
        if not client:
            return None
        try:
            raw = client.get(self.prefix + key)
            return json.loads(raw) if raw else None
        except (redis.RedisError, ValueError) as exc:
            self._err(exc)
            return None

    def set_json(self, key: str, value: Any, ttl: int) -> None:
        client = self._client
        if not client or ttl <= 0:
            return
        try:
            client.set(self.prefix + key, json.dumps(value, default=str), ex=ttl)
        except redis.RedisError as exc:
            self._err(exc)

    def delete(self, key: str) -> None:
        client = self._client
        if not client:
            return
        try:
            client.delete(self.prefix + key)
        except redis.RedisError as exc:
            self._err(exc)

    # -- locks / dedup ---------------------------------------------------------
    @contextmanager
    def lock(self, name: str, ttl: int = 60, wait: float = 0.0) -> Iterator[bool]:
        """Distributed lock. Yields True if acquired, False otherwise.

        Falls back to a process-local lock if Redis is down so a single worker still
        cannot overlap with itself.
        """
        key = self.prefix + "lock:" + name
        token = uuid.uuid4().hex
        acquired = False
        used_redis = False
        deadline = time.monotonic() + wait
        client = self._client
        if client:
            try:
                while True:
                    acquired = bool(client.set(key, token, nx=True, ex=ttl))
                    used_redis = True
                    if acquired or time.monotonic() >= deadline:
                        break
                    time.sleep(0.1)
            except redis.RedisError as exc:
                self._err(exc)
                used_redis = False
        local: threading.Lock | None = None
        if not used_redis:
            with self._local_guard:
                local = self._local_locks.setdefault(name, threading.Lock())
            acquired = local.acquire(timeout=wait) if wait else local.acquire(blocking=False)
        try:
            yield acquired
        finally:
            if acquired:
                if used_redis:
                    self._release(key, token)
                elif local is not None:
                    local.release()

    def _release(self, key: str, token: str) -> None:
        try:
            self.client.eval(_RELEASE_LUA, 1, key, token)
            return
        except redis.ResponseError:
            pass  # scripting unavailable (e.g. restricted Redis) - fall back below
        except redis.RedisError as exc:
            self._err(exc)
            return
        try:
            if self.client.get(key) == token:
                self.client.delete(key)
        except redis.RedisError as exc:
            self._err(exc)

    # -- rate limiting ---------------------------------------------------------
    def allow(self, bucket: str, limit: int, window_seconds: int = 60) -> bool:
        """Fixed-window rate limiter. Returns True if the call is allowed.

        If Redis is unavailable we *deny* calls for buckets that protect external
        sources (fail-safe towards the remote service) - callers pass
        ``bucket`` names starting with ``src:`` for those.
        """
        if limit <= 0:
            return False
        client = self._client
        if not client:
            return not bucket.startswith("src:")
        window = int(time.time() // window_seconds)
        key = f"{self.prefix}rl:{bucket}:{window}"
        try:
            pipe = client.pipeline()
            pipe.incr(key)
            pipe.expire(key, window_seconds + 5)
            count, _ = pipe.execute()
            return int(count) <= limit
        except redis.RedisError as exc:
            self._err(exc)
            return not bucket.startswith("src:")

    def incr(self, key: str, ttl: int = 86400 * 2) -> None:
        client = self._client
        if not client:
            return
        try:
            pipe = client.pipeline()
            pipe.incr(self.prefix + key)
            pipe.expire(self.prefix + key, ttl)
            pipe.execute()
        except redis.RedisError as exc:
            self._err(exc)

    def get_int(self, key: str) -> int:
        client = self._client
        if not client:
            return 0
        try:
            return int(client.get(self.prefix + key) or 0)
        except (redis.RedisError, ValueError) as exc:
            self._err(exc)
            return 0
