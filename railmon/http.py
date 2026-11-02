"""Outbound HTTP client used by all source adapters.

Features: connection pooling, retries with exponential backoff + jitter for
transient failures (connection errors, 5xx, 429 with Retry-After), robots.txt
enforcement (cached), and an identifying User-Agent.
"""
from __future__ import annotations

import random
import ssl
import time
import urllib.robotparser
from dataclasses import dataclass
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter

from .extensions import cache


class RobotsDisallowed(Exception):
    pass


@dataclass
class HttpResult:
    response: requests.Response | None
    error: str | None
    latency_ms: int
    attempts: int

    @property
    def ok(self) -> bool:
        return self.response is not None and self.error is None


class _CipherAdapter(HTTPAdapter):
    """HTTPAdapter that appends extra TLS cipher suites to OpenSSL's defaults.

    Some official Indian Railways hosts only offer TLS 1.2 RSA-key-exchange
    AES-GCM suites, which Python's default cipher list excludes. Certificate
    and hostname verification are unchanged.
    """

    def __init__(self, extra_ciphers: str = "", **kwargs) -> None:
        self.extra_ciphers = extra_ciphers
        super().__init__(**kwargs)

    def _ctx(self) -> ssl.SSLContext:
        ctx = ssl.create_default_context()
        if self.extra_ciphers:
            ctx.set_ciphers("DEFAULT:" + self.extra_ciphers)
        return ctx

    def init_poolmanager(self, *args, **kwargs):
        kwargs["ssl_context"] = self._ctx()
        return super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, proxy, **kwargs):
        kwargs["ssl_context"] = self._ctx()
        return super().proxy_manager_for(proxy, **kwargs)


class HttpClient:
    RETRY_STATUSES = {429, 500, 502, 503, 504}

    def __init__(self, config) -> None:
        self.user_agent = config.get("HTTP_USER_AGENT")
        self.timeout = config.get("HTTP_TIMEOUT_SECONDS", 15)
        self.max_retries = max(1, config.get("HTTP_MAX_RETRIES", 3))
        self.backoff = config.get("HTTP_BACKOFF_BASE_SECONDS", 1.5)
        self.respect_robots = config.get("RESPECT_ROBOTS_TXT", True)
        self.session = requests.Session()
        adapter = _CipherAdapter(
            extra_ciphers=config.get("HTTP_EXTRA_CIPHERS", ""), pool_connections=10, pool_maxsize=20, max_retries=0
        )
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self.session.headers.update({"User-Agent": self.user_agent, "Accept-Language": "en-IN,en;q=0.8"})

    # -- robots.txt -------------------------------------------------------------
    def robots_allowed(self, url: str) -> bool:
        return self.robots_check(url)[0]

    def robots_check(self, url: str) -> tuple[bool, str]:
        """Return (allowed, reason) for fetching ``url`` according to robots.txt.

        Policy (same as major crawlers): 404 -> allowed; 401/403 -> disallowed;
        network error / 5xx -> disallowed (we cannot confirm permission).
        """
        if not self.respect_robots:
            return True, "robots.txt checks disabled"
        parts = urlsplit(url)
        robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
        cache_key = f"robots:{parts.netloc}"
        body = cache.get_json(cache_key)
        if body is None:
            try:
                resp = self.session.get(robots_url, timeout=self.timeout)
                if resp.status_code == 404:
                    body = {"mode": "allow_all"}
                elif resp.status_code in (401, 403):
                    body = {"mode": "deny_all"}
                elif resp.status_code >= 400:
                    body = {"mode": "deny_all", "transient": True, "why": f"robots.txt returned HTTP {resp.status_code}"}
                else:
                    body = {"mode": "parse", "text": resp.text[:200_000]}
            except requests.RequestException as exc:
                body = {"mode": "deny_all", "transient": True, "why": f"robots.txt unreachable ({type(exc).__name__})"}
            cache.set_json(cache_key, body, 900 if body.get("transient") else 86400)
        if body["mode"] == "allow_all":
            return True, "no robots.txt"
        if body["mode"] == "deny_all":
            return False, body.get("why") or "robots.txt access denied"
        rp = urllib.robotparser.RobotFileParser()
        rp.parse(body["text"].splitlines())
        ok = rp.can_fetch(self.user_agent, url)
        return ok, "allowed by robots.txt" if ok else "disallowed by robots.txt"

    # -- requests ----------------------------------------------------------------
    def request(self, method: str, url: str, *, check_robots: bool = True, **kwargs) -> HttpResult:
        if check_robots:
            ok, why = self.robots_check(url)
            if not ok:
                raise RobotsDisallowed(f"not fetching {url}: {why} - treated as not permitted")
        kwargs.setdefault("timeout", self.timeout)
        start = time.monotonic()
        last_error = None
        resp = None
        attempt = 0
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self.session.request(method, url, **kwargs)
                if resp.status_code in self.RETRY_STATUSES and attempt < self.max_retries:
                    last_error = f"HTTP {resp.status_code}"
                    self._sleep(attempt, resp.headers.get("Retry-After"))
                    continue
                last_error = None if resp.status_code < 400 else f"HTTP {resp.status_code}"
                break
            except requests.RequestException as exc:
                resp = None
                last_error = f"{type(exc).__name__}: {exc}"[:500]
                if attempt < self.max_retries:
                    self._sleep(attempt, None)
        latency = int((time.monotonic() - start) * 1000)
        return HttpResult(response=resp, error=last_error, latency_ms=latency, attempts=attempt)

    def get(self, url: str, **kwargs) -> HttpResult:
        return self.request("GET", url, **kwargs)

    def _sleep(self, attempt: int, retry_after: str | None) -> None:
        delay = self.backoff * (2 ** (attempt - 1)) + random.uniform(0, self.backoff / 2 if self.backoff else 0)
        if retry_after and retry_after.isdigit():
            delay = max(delay, min(int(retry_after), 120))
        if delay > 0:
            time.sleep(delay)
