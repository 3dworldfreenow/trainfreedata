"""Internal REST API (``/api/v1``).

Used by the dashboard's JavaScript and by other internal services. Auth:
browser session (+ ``X-CSRF-Token`` for writes) or ``X-Internal-Token``.
Every source-backed endpoint returns the normalised envelope
``{source, fetched_at, status, data, cached, error, data_kind, manual_enquiry}``.
"""
from __future__ import annotations

from datetime import date, datetime

from flask import Blueprint, abort, current_app, g, jsonify, request

from ..extensions import cache, db
from ..models import DISRUPTION_TYPES, DailyReport, MonitoringJob, Pnr, Station, Train, TrainLiveStatus, utcnow
from ..services import disruptions as disruption_svc
from ..services import pnr as pnr_svc
from ..services import reports as report_svc
from ..services import stations as station_svc
from ..services import trains as train_svc
from ..services.health import health_summary
from ..services.sources import manager
from ..timeutil import today_ist
from .auth import admin_required, login_required, public_page

api_bp = Blueprint("api", __name__)


def _date(name: str, default: date | None = None) -> date | None:
    raw = request.args.get(name) or (request.get_json(silent=True) or {}).get(name)
    if not raw:
        return default
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        abort(400, description=f"{name} must be YYYY-MM-DD")


def _iso(dt):
    return dt.isoformat() + "Z" if dt else None


def train_json(t: Train) -> dict:
    return {
        "number": t.number, "name": t.name, "type": t.train_type, "zone": t.zone,
        "source_station_code": t.source_station_code, "destination_station_code": t.destination_station_code,
        "departure_time": t.departure_time, "arrival_time": t.arrival_time, "running_days": t.running_days,
        "distance_km": t.distance_km, "is_monitored": t.is_monitored, "data_source": t.data_source, "data_kind": "static",
    }


def live_json(s: TrainLiveStatus | None) -> dict | None:
    if not s:
        return None
    return {
        "train_number": s.train_number, "journey_date": s.journey_date.isoformat() if s.journey_date else None,
        "data_kind": s.data_kind, "source": s.source.key if s.source else None, "running_status": s.running_status,
        "current_station_code": s.current_station_code, "last_reported_station_code": s.last_reported_station_code,
        "last_reported_at": _iso(s.last_reported_at), "delay_minutes": s.delay_minutes, "eta": _iso(s.eta),
        "etd": _iso(s.etd), "fetched_at": _iso(s.fetched_at), "last_confirmed_at": _iso(s.last_confirmed_at),
    }


def pnr_json(p: Pnr, with_passengers: bool = True) -> dict:
    out = {
        "id": p.id, "pnr": f"******{p.pnr_last4}", "label": p.label, "train_number": p.train_number,
        "train_name": p.train_name, "journey_date": p.journey_date.isoformat() if p.journey_date else None,
        "boarding_station_code": p.boarding_station_code, "travel_class": p.travel_class,
        "chart_status": p.chart_status, "chart_status_changed_at": _iso(p.chart_status_changed_at),
        "monitoring_enabled": p.monitoring_enabled, "last_checked_at": _iso(p.last_checked_at),
        "last_check_result": p.last_check_result, "next_check_at": _iso(p.next_check_at),
        "next_chart_check_at": _iso(p.next_chart_check_at),
    }
    if with_passengers:
        out["passengers"] = [
            {"serial": x.serial, "booking_status": x.booking_status, "booking_code": x.booking_code,
             "current_status": x.current_status, "current_code": x.current_code, "coach": x.coach, "berth": x.berth}
            for x in p.passengers
        ]
    return out


@api_bp.errorhandler(400)
@api_bp.errorhandler(401)
@api_bp.errorhandler(403)
@api_bp.errorhandler(404)
def _err(e):
    return jsonify({"status": "error", "error": getattr(e, "description", str(e))}), e.code


# ---------------------------------------------------------------- system
@api_bp.get("/health")
def health():
    try:
        db.session.execute(db.text("SELECT 1"))
        db_ok = True
    except Exception:  # noqa: BLE001
        db_ok = False
    return jsonify({"status": "ok" if db_ok else "degraded", "database": db_ok, "redis": cache.ping(), "time": _iso(utcnow())}), (200 if db_ok else 503)


@api_bp.get("/stats")
@public_page
def stats():
    s = report_svc.overview_stats()
    s["sources"] = [{"key": x.key, "status": x.last_status, "last_success_at": _iso(x.last_success_at)} for x in s["sources"]]
    s["last_live_update"], s["last_job_success"] = _iso(s["last_live_update"]), _iso(s["last_job_success"])
    return jsonify(s)


# ---------------------------------------------------------------- autocomplete
@api_bp.get("/suggest/stations")
@public_page
def suggest_stations():
    from ..services.suggest import suggest_stations as _s

    resp = jsonify({"items": _s(request.args.get("q", ""), min(int(request.args.get("limit", 8)), 20))})
    resp.headers["Cache-Control"] = "public, max-age=300"
    return resp


@api_bp.get("/suggest/trains")
@public_page
def suggest_trains():
    from ..services.suggest import suggest_trains as _s

    resp = jsonify({"items": _s(request.args.get("q", ""), min(int(request.args.get("limit", 8)), 20))})
    resp.headers["Cache-Control"] = "public, max-age=300"
    return resp


# ---------------------------------------------------------------- trains
@api_bp.get("/trains")
@public_page
def trains():
    per_page = min(int(request.args.get("per_page", 50)), 200)
    rows, total = train_svc.search_trains(request.args.get("q", ""), request.args.get("station", ""),
                                          request.args.get("type", ""), None, request.args.get("sort", "number"),
                                          request.args.get("dir", "asc"), int(request.args.get("page", 1)), per_page)
    status = train_svc.latest_status_for([t.id for t in rows])
    return jsonify({"total": total, "items": [train_json(t) | {"latest_status": live_json(status.get(t.id))} for t in rows]})


@api_bp.get("/trains/between")
@public_page
def trains_between():
    frm, to = request.args.get("from", ""), request.args.get("to", "")
    if not frm or not to:
        abort(400, description="from and to station codes are required")
    return jsonify({"data_kind": "static", "items": [
        train_json(r["train"]) | {"from_departure": r["departure"], "to_arrival": r["arrival"]} for r in train_svc.trains_between(frm, to)
    ]})


@api_bp.get("/trains/<number>")
@public_page
def train(number):
    t = db.session.query(Train).filter_by(number=number).first()
    if not t:
        abort(404, description="train not found in master data")
    latest = train_svc.latest_status_for([t.id]).get(t.id)
    return jsonify({"train": train_json(t), "schedule": manager.fetch("train_schedule", train_number=number),
                    "latest_status": live_json(latest)})


@api_bp.get("/trains/<number>/status")
@public_page
def train_status(number):
    return jsonify(train_svc.refresh_train_status(number, _date("date", today_ist())))


@api_bp.get("/trains/<number>/history")
@public_page
def train_history(number):
    rows = db.session.query(TrainLiveStatus).filter_by(train_number=number).order_by(TrainLiveStatus.id.desc()).limit(200).all()
    return jsonify({"items": [live_json(r) for r in rows]})


@api_bp.get("/delayed")
@public_page
def delayed():
    th = int(request.args.get("min", current_app.config["DELAY_THRESHOLD_MINUTES"]))
    return jsonify({"items": [live_json(r) for r in train_svc.delayed_trains(th, _date("date", today_ist()))]})


# ---------------------------------------------------------------- stations
@api_bp.get("/stations")
@public_page
def stations():
    rows, total = station_svc.search_stations(request.args.get("q", ""), None, int(request.args.get("page", 1)), 50)
    return jsonify({"total": total, "items": [
        {"code": s.code, "name": s.name, "state": s.state, "zone": s.zone, "lat": s.latitude, "lng": s.longitude,
         "is_monitored": s.is_monitored} for s in rows
    ]})


@api_bp.get("/stations/<code>/board")
@public_page
def station_board(code):
    if not db.session.query(Station).filter_by(code=code.upper()).first():
        abort(404, description="unknown station")
    b = station_svc.station_board(code, window_hours=min(int(request.args.get("hours", 2)), 8))
    return jsonify({"live": b["live"], "scheduled": b["scheduled"],
                    "last_snapshot": {"fetched_at": _iso(b["last_snapshot"].fetched_at), "data_kind": b["last_snapshot"].data_kind,
                                      "entries": b["last_snapshot"].entries} if b["last_snapshot"] else None})


# ---------------------------------------------------------------- disruptions & reports
@api_bp.get("/disruptions")
@public_page
def disruptions():
    dtype = request.args.get("type")
    if dtype and dtype not in DISRUPTION_TYPES:
        abort(400, description=f"type must be one of {', '.join(DISRUPTION_TYPES)}")
    d_from = _date("from", today_ist())
    rows, total = disruption_svc.query(dtype, d_from, _date("to", d_from), request.args.get("q", ""), int(request.args.get("page", 1)), 200)
    return jsonify({"total": total, "items": [
        {"train_number": r.train_number, "train_name": r.train_name, "type": r.disruption_type,
         "journey_date": r.journey_date.isoformat(), "from": r.from_station_code, "to": r.to_station_code,
         "affected_from": r.affected_from_code, "affected_to": r.affected_to_code,
         "rescheduled_departure": _iso(r.rescheduled_departure), "rescheduled_by_minutes": r.rescheduled_by_minutes,
         "diverted_route": r.diverted_route, "reason": r.reason, "data_kind": r.data_kind,
         "source": r.source.key if r.source else None, "first_seen_at": _iso(r.first_seen_at), "last_seen_at": _iso(r.last_seen_at)}
        for r in rows
    ]})


@api_bp.post("/disruptions/refresh")
@admin_required
def disruptions_refresh():
    return jsonify(disruption_svc.refresh_disruptions(_date("date", today_ist()), use_cache=False))


@api_bp.get("/reports/daily")
@public_page
def daily_report():
    d = _date("date", today_ist())
    stored = db.session.query(DailyReport).filter_by(report_date=d).first()
    return jsonify({"date": d.isoformat(), "stored": stored.summary if stored else None, "live": report_svc.build_daily_summary(d)})


# ---------------------------------------------------------------- PNR (private)
@api_bp.post("/pnr/enquiry")
@login_required
def pnr_enquiry():
    body = request.get_json(silent=True) or request.form
    try:
        return jsonify(pnr_svc.enquire(body.get("pnr", ""), g.user))
    except pnr_svc.PnrError as exc:
        abort(400, description=str(exc))


@api_bp.get("/pnrs")
@login_required
def pnr_list():
    rows = db.session.query(Pnr).filter_by(user_id=g.user.id).order_by(Pnr.id.desc()).all()
    return jsonify({"items": [pnr_json(p) for p in rows]})


@api_bp.post("/pnrs")
@login_required
def pnr_save():
    body = request.get_json(silent=True) or {}
    try:
        p = pnr_svc.save_pnr(g.user, body.get("pnr", ""), label=body.get("label"), journey_date=_date("journey_date"),
                             train_number=body.get("train_number"), boarding_station_code=body.get("boarding_station_code"),
                             check_interval_minutes=body.get("check_interval_minutes"))
    except pnr_svc.PnrError as exc:
        abort(400, description=str(exc))
    return jsonify(pnr_json(p)), 201


def _own(pnr_id: int) -> Pnr:
    p = db.session.get(Pnr, pnr_id)
    if not p or (p.user_id != g.user.id and g.user.role != "admin"):
        abort(404)
    return p


@api_bp.get("/pnrs/<int:pnr_id>")
@login_required
def pnr_get(pnr_id):
    p = _own(pnr_id)
    hist = [{"checked_at": _iso(h["row"].checked_at), "data_kind": h["row"].data_kind, "chart_status": h["row"].chart_status,
             "changed": h["row"].changed, "change_summary": h["row"].change_summary, "passengers": h["passengers"]}
            for h in pnr_svc.history_rows(p)]
    charts = [{"observed_at": _iso(c.observed_at), "from": c.previous_status, "to": c.chart_status, "data_kind": c.data_kind}
              for c in p.chart_history]
    return jsonify(pnr_json(p) | {"history": hist, "chart_history": charts})


@api_bp.post("/pnrs/<int:pnr_id>/check")
@login_required
def pnr_check(pnr_id):
    return jsonify(pnr_svc.check_saved_pnr(_own(pnr_id), use_cache=False))


@api_bp.post("/pnrs/<int:pnr_id>/manual-status")
@login_required
def pnr_manual(pnr_id):
    try:
        h = pnr_svc.record_manual(_own(pnr_id), request.get_json(silent=True) or request.form)
    except pnr_svc.PnrError as exc:
        abort(400, description=str(exc))
    return jsonify({"changed": h.changed, "change_summary": h.change_summary})


@api_bp.delete("/pnrs/<int:pnr_id>")
@login_required
def pnr_delete(pnr_id):
    pnr_svc.delete_pnr(_own(pnr_id))
    return jsonify({"deleted": True})


# ---------------------------------------------------------------- sources & jobs (admin)
@api_bp.get("/sources")
@public_page
def sources():
    return jsonify({"items": [
        {"key": s["source"].key, "name": s["source"].name, "enabled": s["source"].enabled,
         "automated_access": s["source"].automated_access, "status": s["source"].last_status,
         "last_success_at": _iso(s["source"].last_success_at), "last_failure_at": _iso(s["source"].last_failure_at),
         "last_error": s["source"].last_error, "capabilities": s["capabilities"], "availability_24h_pct": s["availability_pct"]}
        for s in health_summary(24)
    ]})


@api_bp.get("/jobs")
@admin_required
def jobs():
    rows = db.session.query(MonitoringJob).order_by(MonitoringJob.job_key).all()
    return jsonify({"items": [
        {"key": j.job_key, "name": j.name, "enabled": j.enabled, "trigger": j.trigger_type, "interval_seconds": j.interval_seconds,
         "cron": j.cron, "last_status": j.last_status, "last_message": j.last_message, "last_started_at": _iso(j.last_started_at),
         "last_success_at": _iso(j.last_success_at), "next_run_at": _iso(j.next_run_at), "run_count": j.run_count,
         "failure_count": j.failure_count} for j in rows
    ]})


@api_bp.post("/jobs/<key>/<action>")
@admin_required
def job_action(key, action):
    job = db.session.query(MonitoringJob).filter_by(job_key=key).first()
    if not job:
        abort(404)
    if action == "pause":
        job.enabled = False
    elif action == "resume":
        job.enabled = True
    elif action == "run":
        job.run_requested_at = utcnow()
    else:
        abort(400, description="action must be pause, resume or run")
    db.session.commit()
    return jsonify({"key": key, "enabled": job.enabled, "run_requested": job.run_requested_at is not None})
