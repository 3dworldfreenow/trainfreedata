"""Dashboard statistics and daily summaries."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from flask import current_app
from sqlalchemy import func

from ..extensions import cache, db
from ..models import (
    ApiSource,
    ChartStatusHistory,
    DailyReport,
    MonitoringJob,
    Pnr,
    PnrStatusHistory,
    Station,
    Train,
    TrainDisruption,
    TrainLiveStatus,
)
from ..timeutil import IST, today_ist, utcnow
from .trains import latest_statuses


def _ist_day_bounds(d: date) -> tuple[datetime, datetime]:
    from datetime import timezone

    s = datetime(d.year, d.month, d.day, tzinfo=IST).astimezone(timezone.utc).replace(tzinfo=None)
    return s, s + timedelta(days=1)


def overview_stats() -> dict:
    cfg = current_app.config
    today = today_ist()
    start, end = _ist_day_bounds(today)
    latest = latest_statuses(5000)
    today_status = [s for s in latest if s.journey_date == today]
    stale_cut = utcnow() - timedelta(minutes=cfg["STALE_AFTER_MINUTES"])
    last_live = db.session.query(func.max(TrainLiveStatus.last_confirmed_at)).scalar()
    last_job_ok = db.session.query(func.max(MonitoringJob.last_success_at)).scalar()
    sources = db.session.query(ApiSource).order_by(ApiSource.priority).all()
    return {
        "trains_total": db.session.query(func.count(Train.id)).scalar() or 0,
        "stations_total": db.session.query(func.count(Station.id)).scalar() or 0,
        "trains_monitored": db.session.query(func.count(Train.id)).filter(Train.is_monitored.is_(True)).scalar() or 0,
        "stations_monitored": db.session.query(func.count(Station.id)).filter(Station.is_monitored.is_(True)).scalar() or 0,
        "live_today": len(today_status),
        "live_fresh": sum(1 for s in today_status if s.last_confirmed_at >= stale_cut),
        "delayed_today": sum(1 for s in today_status if (s.delay_minutes or 0) >= cfg["DELAY_THRESHOLD_MINUTES"]),
        "disruptions_today": db.session.query(func.count(TrainDisruption.id)).filter(TrainDisruption.journey_date == today).scalar() or 0,
        "pnr_checks_today": cache.get_int(f"stats:pnr_checks:{utcnow():%Y-%m-%d}")
        or (db.session.query(func.count(PnrStatusHistory.id)).filter(PnrStatusHistory.checked_at >= start).scalar() or 0),
        "pnrs_saved": db.session.query(func.count(Pnr.id)).scalar() or 0,
        "chart_updates_today": db.session.query(func.count(ChartStatusHistory.id)).filter(ChartStatusHistory.observed_at >= start).scalar() or 0,
        "last_live_update": last_live,
        "last_job_success": last_job_ok,
        "sources": sources,
        "redis_ok": cache.ping(),
    }


def build_daily_summary(d: date) -> dict:
    start, end = _ist_day_bounds(d)
    disruptions = dict(
        db.session.query(TrainDisruption.disruption_type, func.count(TrainDisruption.id))
        .filter(TrainDisruption.journey_date == d)
        .group_by(TrainDisruption.disruption_type)
        .all()
    )
    statuses = [s for s in latest_statuses(20000) if s.journey_date == d]
    delays = [s.delay_minutes for s in statuses if s.delay_minutes is not None]
    threshold = current_app.config["DELAY_THRESHOLD_MINUTES"]
    worst = sorted((s for s in statuses if s.delay_minutes), key=lambda s: -s.delay_minutes)[:10]
    by_kind = {}
    for s in statuses:
        by_kind[s.data_kind] = by_kind.get(s.data_kind, 0) + 1
    return {
        "date": d.isoformat(),
        "disruptions": disruptions,
        "disruptions_total": sum(disruptions.values()),
        "trains_with_status": len(statuses),
        "status_by_data_kind": by_kind,
        "delayed_count": sum(1 for x in delays if x >= threshold),
        "avg_delay_minutes": round(sum(delays) / len(delays), 1) if delays else None,
        "max_delay_minutes": max(delays) if delays else None,
        "worst_delays": [{"train_number": s.train_number, "delay_minutes": s.delay_minutes, "data_kind": s.data_kind} for s in worst],
        "pnr_checks": db.session.query(func.count(PnrStatusHistory.id)).filter(PnrStatusHistory.checked_at.between(start, end)).scalar() or 0,
        "pnr_changes": db.session.query(func.count(PnrStatusHistory.id)).filter(PnrStatusHistory.checked_at.between(start, end), PnrStatusHistory.changed.is_(True)).scalar() or 0,
        "chart_updates": db.session.query(func.count(ChartStatusHistory.id)).filter(ChartStatusHistory.observed_at.between(start, end)).scalar() or 0,
        "delay_threshold_minutes": threshold,
    }


def save_daily_summary(d: date) -> DailyReport:
    summary = build_daily_summary(d)
    row = db.session.query(DailyReport).filter_by(report_date=d).first()
    if row is None:
        row = DailyReport(report_date=d, summary=summary)
        db.session.add(row)
    else:
        row.summary, row.generated_at = summary, utcnow()
    db.session.commit()
    return row
