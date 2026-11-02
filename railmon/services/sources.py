"""SourceManager: runs adapters safely and returns normalised results.

For each request it:
1. serves an identical recent result from the Redis cache (``cached = true``);
2. takes a single-flight lock so concurrent identical requests share one fetch;
3. tries enabled adapters supporting the capability in priority order, each
   behind a per-source rate limit;
4. isolates failures (exceptions become ``error`` results and are logged);
5. records source health and returns the first successful result, otherwise a
   ``manual_required`` result listing official manual-enquiry links, or an
   ``unavailable`` result explaining why.
"""
from __future__ import annotations

import hashlib
import time
from datetime import date
from typing import Any

from flask import current_app

from ..adapters import REGISTRY
from ..adapters.base import (
    ERROR,
    MANUAL_REQUIRED,
    METHOD_FOR,
    NOT_FOUND,
    OK,
    RATE_LIMITED,
    UNAVAILABLE,
    AdapterResponse,
    NotSupported,
)
from ..extensions import cache, db
from ..http import HttpClient, RobotsDisallowed
from ..models import ApiSource, SourceHealth, utcnow
from ..syslog import log_event

_TTL_KEYS = {
    "train_status": "CACHE_TTL_TRAIN_STATUS",
    "station_board": "CACHE_TTL_STATION_BOARD",
    "pnr_status": "CACHE_TTL_PNR",
    "chart_status": "CACHE_TTL_PNR",
    "cancelled_trains": "CACHE_TTL_DISRUPTIONS",
    "rescheduled_trains": "CACHE_TTL_DISRUPTIONS",
    "diverted_trains": "CACHE_TTL_DISRUPTIONS",
    "train_schedule": "CACHE_TTL_STATIC",
    "station_schedule": "CACHE_TTL_STATION_BOARD",
}
# capabilities whose arguments are sensitive and must not appear in cache keys/logs
_SENSITIVE = {"pnr_status", "chart_status"}

_http_client: HttpClient | None = None


def http_client() -> HttpClient:
    global _http_client
    if _http_client is None:
        _http_client = HttpClient(current_app.config)
    return _http_client


def source_snapshot(src: ApiSource) -> dict:
    return {
        "id": src.id,
        "key": src.key,
        "name": src.name,
        "adapter": src.adapter,
        "base_url": src.base_url,
        "automated_access": src.automated_access,
        "rate_limit_per_minute": src.rate_limit_per_minute,
        "config": src.config or {},
        "enabled": src.enabled,
        "priority": src.priority,
    }


def build_adapter(src: ApiSource | dict):
    snap = src if isinstance(src, dict) else source_snapshot(src)
    cls = REGISTRY.get(snap["adapter"])
    if cls is None:
        return None
    return cls(snap, http_client(), current_app.config)


def envelope(
    source: str | None,
    status: str,
    data: Any = None,
    error: str | None = None,
    cached: bool = False,
    data_kind: str | None = None,
    manual: list | None = None,
    fetched_at: str | None = None,
    source_id: int | None = None,
) -> dict:
    return {
        "source": source,
        "source_id": source_id,
        "fetched_at": fetched_at or utcnow().isoformat() + "Z",
        "status": status,
        "data": data,
        "cached": cached,
        "error": error,
        "data_kind": data_kind,
        "manual_enquiry": manual or [],
    }


def _arg_repr(capability: str, kwargs: dict) -> str:
    raw = "|".join(f"{k}={v.isoformat() if isinstance(v, date) else v}" for k, v in sorted(kwargs.items()))
    if capability in _SENSITIVE:
        # never place raw PNRs in Redis keys
        from ..security import crypto

        return crypto.pnr_hash(raw)
    return hashlib.sha1(raw.encode()).hexdigest()


class SourceManager:
    def enabled_sources(self, capability: str) -> list[ApiSource]:
        rows = (
            db.session.query(ApiSource)
            .filter(ApiSource.enabled.is_(True))
            .order_by(ApiSource.priority, ApiSource.id)
            .all()
        )
        out = []
        for src in rows:
            adapter = build_adapter(src)
            if adapter is not None and adapter.supports(capability):
                out.append(src)
        return out

    def automated_sources(self, capability: str) -> list[ApiSource]:
        """Enabled sources that can actually retrieve data automatically for ``capability``."""
        out = []
        for src in self.enabled_sources(capability):
            adapter = build_adapter(src)
            if adapter.manual_only:
                continue
            if not adapter.local_only and src.automated_access != "permitted":
                continue
            out.append(src)
        return out

    def manual_links(self, capability: str, **kwargs) -> list[dict]:
        """Official manual-enquiry links for a capability (no network calls are made)."""
        out = []
        for src in self.enabled_sources(capability):
            adapter = build_adapter(src)
            if not adapter.manual_only:
                continue
            try:
                resp = getattr(adapter, METHOD_FOR[capability])(**kwargs)
            except Exception:  # noqa: BLE001
                continue
            if resp.status == MANUAL_REQUIRED:
                out.append({"source": src.key, "name": src.name, "url": resp.manual_url,
                            "instructions": resp.manual_instructions, "reason": resp.error})
        return out

    def fetch(self, capability: str, *, use_cache: bool = True, **kwargs) -> dict:
        if capability not in METHOD_FOR:
            raise ValueError(f"unknown capability {capability}")
        cache_key = f"res:{capability}:{_arg_repr(capability, kwargs)}"
        ttl = int(current_app.config.get(_TTL_KEYS[capability], 300))

        if use_cache:
            hit = cache.get_json(cache_key)
            if hit:
                hit["cached"] = True
                return hit

        with cache.lock("fetch:" + cache_key, ttl=90, wait=15) as acquired:
            if use_cache or not acquired:
                hit = cache.get_json(cache_key)
                if hit:
                    hit["cached"] = True
                    return hit
            if not acquired:
                return envelope(None, ERROR, error="identical request already in progress; try again shortly")
            result = self._fetch_uncached(capability, kwargs)
            if result["status"] in (OK, NOT_FOUND):
                cache.set_json(cache_key, result, ttl if result["status"] == OK else min(ttl, 120))
            return result

    def _fetch_uncached(self, capability: str, kwargs: dict) -> dict:
        sources = self.enabled_sources(capability)
        if not sources:
            return envelope(None, UNAVAILABLE, error=f"no enabled source supports '{capability}'")
        manual: list[dict] = []
        errors: list[str] = []
        for src in sources:
            snap = source_snapshot(src)
            adapter = build_adapter(snap)
            # rate limits protect remote services; manual/local adapters make no requests
            needs_limit = not (adapter.manual_only or adapter.local_only)
            if needs_limit and not cache.allow(f"src:{snap['key']}", snap["rate_limit_per_minute"]):
                errors.append(f"{snap['key']}: rate limit reached")
                self._record(snap, capability, AdapterResponse(status=RATE_LIMITED, network=False))
                continue
            started = time.monotonic()
            try:
                resp: AdapterResponse = getattr(adapter, METHOD_FOR[capability])(**kwargs)
            except NotSupported:
                continue
            except RobotsDisallowed as exc:
                resp = AdapterResponse(status=ERROR, error=str(exc))
            except Exception as exc:  # noqa: BLE001 - isolate any adapter failure
                db.session.rollback()
                resp = AdapterResponse(status=ERROR, error=f"{type(exc).__name__}: {exc}"[:500])
                log_event("ERROR", f"adapter.{snap['key']}", f"{capability} raised", error=resp.error)
            if resp.latency_ms is None:
                resp.latency_ms = int((time.monotonic() - started) * 1000)
            self._record(snap, capability, resp)

            if resp.status in (OK, NOT_FOUND):
                return envelope(
                    snap["key"], resp.status, data=resp.data, error=resp.error, data_kind=resp.data_kind,
                    source_id=snap["id"],
                )
            if resp.status == MANUAL_REQUIRED:
                manual.append(
                    {
                        "source": snap["key"],
                        "name": snap["name"],
                        "url": resp.manual_url,
                        "instructions": resp.manual_instructions,
                        "reason": resp.error,
                    }
                )
            elif resp.error:
                errors.append(f"{snap['key']}: {resp.error}")

        if manual:
            return envelope(
                None,
                MANUAL_REQUIRED,
                error="No source offers permitted automated access for this request; manual enquiry required."
                + (f" Errors: {'; '.join(errors)}" if errors else ""),
                manual=manual,
                data_kind="manual",
            )
        return envelope(None, UNAVAILABLE, error="; ".join(errors) or "all sources failed")

    # -- health bookkeeping -------------------------------------------------------
    def _record(self, snap: dict, capability: str, resp: AdapterResponse) -> None:
        if not resp.network and resp.status not in (RATE_LIMITED,):
            return  # nothing touched the network (static/manual) - no health signal
        try:
            src = db.session.get(ApiSource, snap["id"])
            now = utcnow()
            success = resp.status in (OK, NOT_FOUND)
            status = "up" if success else ("rate_limited" if resp.status == RATE_LIMITED else "down")
            db.session.add(
                SourceHealth(
                    source_id=snap["id"],
                    checked_at=now,
                    check_type="request",
                    status=status,
                    capability=capability,
                    http_status=resp.http_status,
                    latency_ms=resp.latency_ms,
                    error=(resp.error or None) if not success else None,
                )
            )
            if src:
                if success:
                    src.last_success_at, src.consecutive_failures, src.last_status = now, 0, "up"
                elif resp.status != RATE_LIMITED:
                    src.last_failure_at, src.last_error = now, resp.error
                    src.consecutive_failures += 1
                    src.last_status = "degraded" if src.consecutive_failures < 3 else "down"
            db.session.commit()
            if not success and resp.status != RATE_LIMITED:
                log_event("WARNING", f"source.{snap['key']}", f"{capability} failed", error=resp.error)
        except Exception as exc:  # noqa: BLE001
            db.session.rollback()
            log_event("ERROR", "sources", "failed to record source health", error=str(exc))


manager = SourceManager()
