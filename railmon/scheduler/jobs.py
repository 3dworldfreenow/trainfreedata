"""Scheduled job implementations. Each returns a short status message."""
from __future__ import annotations

from datetime import timedelta

from flask import current_app

from ..extensions import db
from ..models import MonitoringJob, Pnr, SourceHealth, Station, SystemLog, Train, TrainLiveStatus
from ..services import chart as chart_svc
from ..services import disruptions as disruption_svc
from ..services import health as health_svc
from ..services import pnr as pnr_svc
from ..services import reports as report_svc
from ..services import stations as station_svc
from ..services import trains as train_svc
from ..services.sources import manager
from ..timeutil import today_ist, utcnow


class Skipped(Exception):
    """Raised when a job has nothing it can legitimately do (e.g. no automated source)."""


def _cfg(job: MonitoringJob, key: str, default):
    return (job.config or {}).get(key, default)


def train_status(job: MonitoringJob) -> str:
    if not manager.automated_sources("train_status"):
        raise Skipped("no enabled source with permitted automated access for train_status")
    trains = (
        db.session.query(Train).filter(Train.is_monitored.is_(True)).order_by(Train.number)
        .limit(_cfg(job, "max_trains_per_run", 50)).all()
    )
    ok = 0
    for t in trains:
        res = train_svc.refresh_train_status(t.number)
        ok += res["status"] == "ok"
    return f"{ok}/{len(trains)} trains refreshed"


def station_boards(job: MonitoringJob) -> str:
    if not manager.automated_sources("station_board"):
        raise Skipped("no enabled source with permitted automated access for station_board")
    stations = (
        db.session.query(Station).filter(Station.is_monitored.is_(True)).order_by(Station.code)
        .limit(_cfg(job, "max_stations_per_run", 20)).all()
    )
    ok = 0
    for s in stations:
        res = station_svc.station_board(s.code, window_hours=_cfg(job, "window_hours", 2))
        ok += res["live"]["status"] == "ok"
    return f"{ok}/{len(stations)} boards refreshed"


def disruptions(job: MonitoringJob) -> str:
    caps = ("cancelled_trains", "rescheduled_trains", "diverted_trains")
    if not any(manager.automated_sources(c) for c in caps):
        raise Skipped("no enabled source with permitted automated access for disruption reports")
    msgs = []
    for offset in range(0, int(_cfg(job, "days_ahead", 1)) + 1):
        d = today_ist() + timedelta(days=offset)
        summary = disruption_svc.refresh_disruptions(d)
        stored = sum(v["stored"] for v in summary.values())
        msgs.append(f"{d}: {stored} records")
    return "; ".join(msgs)


def pnr_checks(job: MonitoringJob) -> str:
    if not manager.automated_sources("pnr_status"):
        raise Skipped("no enabled source with permitted automated access for pnr_status (manual entry only)")
    now = utcnow()
    due = (
        db.session.query(Pnr)
        .filter(Pnr.monitoring_enabled.is_(True), Pnr.next_check_at.isnot(None), Pnr.next_check_at <= now)
        .order_by(Pnr.next_check_at)
        .limit(current_app.config["PNR_MAX_CHECKS_PER_RUN"])
        .all()
    )
    ok = 0
    for p in due:
        ok += pnr_svc.check_saved_pnr(p)["status"] == "ok"
    return f"{ok}/{len(due)} PNRs checked"


def chart_checks(job: MonitoringJob) -> str:
    if not manager.automated_sources("chart_status"):
        raise Skipped("no enabled source with permitted automated access for chart_status (manual entry only)")
    now = utcnow()
    due = (
        db.session.query(Pnr)
        .filter(Pnr.monitoring_enabled.is_(True), Pnr.chart_status != "prepared",
                Pnr.next_chart_check_at.isnot(None), Pnr.next_chart_check_at <= now)
        .order_by(Pnr.next_chart_check_at)
        .limit(_cfg(job, "max_per_run", 20))
        .all()
    )
    ok = 0
    for p in due:
        ok += chart_svc.check_chart(p)["status"] == "ok"
    return f"{ok}/{len(due)} chart checks"


def source_health(job: MonitoringJob) -> str:
    res = health_svc.probe_all()
    return ", ".join(f"{k}={v}" for k, v in res.items())


def daily_summary(job: MonitoringJob) -> str:
    today = today_ist()
    report_svc.save_daily_summary(today - timedelta(days=1))
    report_svc.save_daily_summary(today)
    return f"summaries saved for {today - timedelta(days=1)} and {today}"


def retention(job: MonitoringJob) -> str:
    cfg = current_app.config
    now = utcnow()
    purged = pnr_svc.purge_expired()
    logs = db.session.query(SystemLog).filter(SystemLog.created_at < now - timedelta(days=cfg["LOG_RETENTION_DAYS"])).delete()
    health = db.session.query(SourceHealth).filter(SourceHealth.checked_at < now - timedelta(days=30)).delete()
    live = db.session.query(TrainLiveStatus).filter(
        TrainLiveStatus.fetched_at < now - timedelta(days=cfg["LIVE_STATUS_RETENTION_DAYS"])
    ).delete()
    db.session.commit()
    return f"purged pnrs={purged} logs={logs} health={health} live={live}"


JOBS = {
    "train_status": train_status,
    "station_boards": station_boards,
    "disruptions": disruptions,
    "pnr_checks": pnr_checks,
    "chart_checks": chart_checks,
    "source_health": source_health,
    "daily_summary": daily_summary,
    "retention": retention,
}
