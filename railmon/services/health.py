"""Source health probes (scheduled) and summaries for the dashboard."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func

from ..extensions import cache, db
from ..models import ApiSource, SourceHealth
from ..syslog import log_event
from ..timeutil import utcnow
from .sources import build_adapter, source_snapshot


def probe_all() -> dict:
    results = {}
    for src in db.session.query(ApiSource).order_by(ApiSource.priority).all():
        results[src.key] = probe_source(src)
    return results


def probe_source(src: ApiSource) -> str:
    now = utcnow()
    if not src.enabled:
        status, hr = "disabled", None
    else:
        adapter = build_adapter(source_snapshot(src))
        if adapter is None:
            status, hr = "down", None
            src.last_error = f"unknown adapter '{src.adapter}'"
        else:
            # one probe per source per 5 minutes, across all workers
            if not cache.allow(f"src:{src.key}:probe", 1, 300):
                return src.last_status
            try:
                hr = adapter.health_check()
                status = hr.status
            except Exception as exc:  # noqa: BLE001
                db.session.rollback()
                hr = None
                status = "down"
                src.last_error = f"{type(exc).__name__}: {exc}"[:500]
    db.session.add(
        SourceHealth(
            source_id=src.id,
            checked_at=now,
            check_type="probe",
            status=status,
            http_status=getattr(hr, "http_status", None),
            latency_ms=getattr(hr, "latency_ms", None),
            error=getattr(hr, "error", None) or (src.last_error if status == "down" else None),
        )
    )
    if status in ("up", "manual_only", "not_applicable"):
        src.last_success_at = now
        src.consecutive_failures = 0
    elif status == "down":
        src.last_failure_at = now
        src.consecutive_failures += 1
        if hr and hr.error:
            src.last_error = hr.error
    # keep the more informative state (e.g. 'manual_only') visible
    src.last_status = status
    db.session.commit()
    if status == "down":
        log_event("WARNING", f"health.{src.key}", "source probe failed", error=src.last_error)
    return status


def health_summary(hours: int = 24) -> list[dict]:
    since = utcnow() - timedelta(hours=hours)
    stats = dict(
        (sid, (total, good))
        for sid, total, good in db.session.query(
            SourceHealth.source_id,
            func.count(SourceHealth.id),
            func.sum(db.case((SourceHealth.status.in_(["up", "manual_only", "not_applicable"]), 1), else_=0)),
        )
        .filter(SourceHealth.checked_at >= since)
        .group_by(SourceHealth.source_id)
        .all()
    )
    out = []
    for src in db.session.query(ApiSource).order_by(ApiSource.priority).all():
        total, good = stats.get(src.id, (0, 0))
        adapter = build_adapter(source_snapshot(src))
        out.append(
            {
                "source": src,
                "capabilities": sorted(adapter.capabilities) if adapter else [],
                "checks": total,
                "availability_pct": round(100.0 * (good or 0) / total, 1) if total else None,
            }
        )
    return out
