"""Chart-preparation monitoring.

Rules:
* Chart status changes ONLY when a source (or the user, from an official
  channel) explicitly reports it. ``unknown`` never overwrites a known status.
* Journey date / scheduled departure are used solely to decide *how often* to
  check, never to infer that a chart has been prepared.
* Every observed change is stored in ``chart_status_history`` with its timestamp.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from flask import current_app

from ..adapters.base import OK
from ..extensions import db
from ..models import ChartStatusHistory, Pnr
from ..normalize import chart_status as normalize_chart
from ..security import crypto
from ..syslog import log_event
from ..timeutil import utcnow
from .sources import manager


def apply_chart_status(pnr: Pnr, reported, source_id: int | None, data_kind: str, observed_at: datetime | None = None) -> bool:
    """Apply a reported chart status. Returns True if it changed."""
    new = normalize_chart(reported)
    if new == "unknown" or new == pnr.chart_status:
        return False
    observed_at = observed_at or utcnow()
    db.session.add(
        ChartStatusHistory(
            pnr_id=pnr.id, observed_at=observed_at, previous_status=pnr.chart_status, chart_status=new,
            source_id=source_id, data_kind=data_kind,
        )
    )
    pnr.chart_status = new
    pnr.chart_status_changed_at = observed_at
    pnr.chart_status_source = data_kind
    return True


def departure_reference(pnr: Pnr) -> datetime | None:
    if pnr.scheduled_departure_at:
        return pnr.scheduled_departure_at
    if pnr.journey_date:
        from ..timeutil import ist_to_utc

        return ist_to_utc(pnr.journey_date, "00:00")
    return None


def next_chart_check(pnr: Pnr, now: datetime | None = None) -> datetime | None:
    """Adaptive chart-check schedule; tighter as departure approaches."""
    now = now or utcnow()
    if not pnr.monitoring_enabled or pnr.chart_status == "prepared":
        return None
    dep = departure_reference(pnr)
    if dep is None:
        return now + timedelta(hours=6)
    hours = (dep - now).total_seconds() / 3600
    floor = current_app.config.get("CHART_MIN_CHECK_INTERVAL_MINUTES", 15)
    if hours > 24:
        return dep - timedelta(hours=24)
    if hours > 6:
        step = 120
    elif hours > 2:
        step = 30
    elif hours > -2:
        step = 15
    else:
        return None  # train departed > 2h ago
    return now + timedelta(minutes=max(step, floor))


def check_chart(pnr: Pnr) -> dict:
    number = crypto.decrypt(pnr.pnr_encrypted)
    if not number:
        return {"status": "error", "error": "cannot decrypt PNR (wrong DATA_ENCRYPTION_KEY?)"}
    res = manager.fetch("chart_status", pnr_number=number)
    now = utcnow()
    if res["status"] == OK and isinstance(res.get("data"), dict):
        changed = apply_chart_status(pnr, res["data"].get("chart_status"), res.get("source_id"), "cached" if res.get("cached") else (res.get("data_kind") or "live"), now)
        if changed:
            log_event("INFO", "chart", "chart status changed", pnr=f"******{pnr.pnr_last4}", status=pnr.chart_status)
    pnr.next_chart_check_at = next_chart_check(pnr, now)
    db.session.commit()
    return {"status": res["status"], "error": res.get("error")}
