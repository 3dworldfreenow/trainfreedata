from unittest import mock

import redis
import requests

from railmon.cache import RedisCache
from railmon.extensions import cache
from railmon.http import HttpClient


def test_lock_is_exclusive(app):
    with cache.lock("x", ttl=30) as first:
        assert first
        with cache.lock("x", ttl=30) as second:
            assert not second
    with cache.lock("x", ttl=30) as again:
        assert again


def test_rate_limit(app):
    assert all(cache.allow("src:t", 3) for _ in range(3))
    assert not cache.allow("src:t", 3)


def test_json_cache_roundtrip(app):
    cache.set_json("k", {"a": 1}, 60)
    assert cache.get_json("k") == {"a": 1}


def test_redis_down_degrades_gracefully():
    c = RedisCache()
    c.client = redis.Redis(host="127.0.0.1", port=1, socket_connect_timeout=0.2, socket_timeout=0.2)
    assert c.get_json("k") is None
    c.set_json("k", {"a": 1}, 60)  # no exception
    with c.lock("y") as ok:  # falls back to a process-local lock
        assert ok
        with c.lock("y") as ok2:
            assert not ok2
    # external sources are protected (fail-safe), internal buckets allowed
    assert c.allow("src:ntes", 5) is False
    assert c.allow("user:1:pnr", 5) is True
    assert c.ping() is False


def _resp(code, text="", headers=None):
    r = requests.Response()
    r.status_code = code
    r._content = text.encode()
    r.headers.update(headers or {})
    return r


def test_http_retries_with_backoff(app):
    client = HttpClient({**app.config, "HTTP_MAX_RETRIES": 3, "HTTP_BACKOFF_BASE_SECONDS": 0.0, "RESPECT_ROBOTS_TXT": False})
    with mock.patch.object(client.session, "request", side_effect=[_resp(503), requests.ConnectionError("x"), _resp(200, "ok")]) as m:
        res = client.get("https://example.org/a")
    assert m.call_count == 3 and res.ok and res.attempts == 3


def test_http_gives_up(app):
    client = HttpClient({**app.config, "HTTP_MAX_RETRIES": 2, "HTTP_BACKOFF_BASE_SECONDS": 0.0, "RESPECT_ROBOTS_TXT": False})
    with mock.patch.object(client.session, "request", side_effect=[_resp(503), _resp(503)]):
        res = client.get("https://example.org/a")
    assert not res.ok and res.error == "HTTP 503"


def test_robots_rules(app):
    client = HttpClient({**app.config, "RESPECT_ROBOTS_TXT": True})
    robots = "User-agent: *\nDisallow: /private/\n"
    with mock.patch.object(client.session, "get", return_value=_resp(200, robots)):
        assert client.robots_allowed("https://example.org/public")
        assert not client.robots_allowed("https://example.org/private/x")
    cache.client.flushall()
    with mock.patch.object(client.session, "get", side_effect=requests.ConnectionError("down")):
        ok, why = client.robots_check("https://unreachable.example/x")
        assert not ok and "unreachable" in why
    cache.client.flushall()
    with mock.patch.object(client.session, "get", return_value=_resp(404)):
        assert client.robots_allowed("https://norobots.example/x")
