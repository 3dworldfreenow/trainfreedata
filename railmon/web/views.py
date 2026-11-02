"""Dashboard pages."""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta

from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, url_for
from sqlalchemy import func

from ..extensions import db
from ..models import (
    DISRUPTION_TYPES,
    ApiSource,
    ChartStatusHistory,
    DailyReport,
    MonitoringJob,
    Pnr,
    SourceHealth,
    Station,
    SystemLog,
    Train,
    TrainLiveStatus,
)
from ..services import disruptions as disruption_svc
from ..services import pnr as pnr_svc
from ..services import reports as report_svc
from ..services import stations as station_svc
from ..services import trains as train_svc
from ..services.health import health_summary
from ..services.sources import manager
from ..timeutil import today_ist, utcnow
from .auth import admin_required, login_required, public_page

views_bp = Blueprint("views", __name__)


def _date_arg(name: str, default: date | None = None) -> date | None:
    raw = request.values.get(name)
    if not raw:
        return default
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return default


def _page() -> int:
    try:
        return max(1, int(request.args.get("page", 1)))
    except ValueError:
        return 1


def _pages(total: int, per_page: int) -> int:
    return max(1, math.ceil(total / per_page))


def _manual_source_id() -> int | None:
    return disruption_svc.manual_source_id()


# ------------------------------------------------------------------ home & search
@views_bp.route("/")
@public_page
def home():
    from ..services.suggest import display_name

    popular = [("NDLS", "BCT"), ("HWH", "NDLS"), ("SBC", "MAS"), ("CSMT", "PUNE"), ("SC", "SBC")]
    names = dict(db.session.query(Station.code, Station.name).filter(Station.code.in_({c for p in popular for c in p})).all())
    popular = [(a, b, display_name(names.get(a, a)), display_name(names.get(b, b))) for a, b in popular if a in names and b in names]
    return render_template("pages/home.html", today=today_ist(), popular=popular, tab=request.args.get("tab", "route"))


@views_bp.route("/search")
@public_page
def search():
    from ..services.suggest import display_name, resolve_station

    frm, to = resolve_station(request.args.get("from")), resolve_station(request.args.get("to"))
    jd = _date_arg("date", today_ist())
    errors = []
    if not frm:
        errors.append(f"Could not find the 'From' station \"{request.args.get('from', '')}\".")
    if not to:
        errors.append(f"Could not find the 'To' station \"{request.args.get('to', '')}\".")
    if frm and to and frm.code == to.code:
        errors.append("'From' and 'To' are the same station.")
    results = []
    if not errors:
        for r in train_svc.trains_between(frm.code, to.code, limit=300):
            r["runs"] = train_svc.runs_on(r["train"].running_days, jd.weekday())
            results.append(r)
        results = [r for r in results if r["runs"] is not False]
    return render_template(
        "pages/search.html", frm=frm, to=to, jd=jd, results=results, errors=errors,
        frm_name=display_name(frm.name) if frm else "", to_name=display_name(to.name) if to else "",
        types=sorted({r["train"].train_type for r in results if r["train"].train_type}),
    )


@views_bp.route("/go/train")
@public_page
def go_train():
    from ..services.suggest import resolve_train

    number = resolve_train(request.args.get("q"))
    if not number:
        flash(f"No train found for \"{request.args.get('q', '')}\". Try the train number.", "warning")
        return redirect(url_for("views.home", tab="train"))
    return redirect(url_for("views.train_detail", number=number, check=request.args.get("check") or None))


@views_bp.route("/go/station")
@public_page
def go_station():
    from ..services.suggest import resolve_station

    st = resolve_station(request.args.get("q"))
    if not st:
        flash(f"No station found for \"{request.args.get('q', '')}\".", "warning")
        return redirect(url_for("views.home", tab="station"))
    return redirect(url_for("views.station_board", code=st.code))


# ------------------------------------------------------------------ 1. overview (monitoring dashboard)
@views_bp.route("/dashboard")
@public_page
def overview():
    stats = report_svc.overview_stats()
    jobs = db.session.query(MonitoringJob).order_by(MonitoringJob.job_key).all()
    recent_changes = []
    if g.user:
        recent_changes = (
            db.session.query(ChartStatusHistory, Pnr)
            .join(Pnr, Pnr.id == ChartStatusHistory.pnr_id)
            .filter(Pnr.user_id == g.user.id)
            .order_by(ChartStatusHistory.observed_at.desc())
            .limit(5)
            .all()
        )
    automated = {cap: bool(manager.automated_sources(cap)) for cap in ("train_status", "station_board", "pnr_status", "chart_status", "cancelled_trains")}
    return render_template("pages/overview.html", stats=stats, jobs=jobs, recent_changes=recent_changes, automated=automated)


# ------------------------------------------------------------------ 2. train monitor
@views_bp.route("/trains")
@public_page
def trains():
    per_page = 50
    q, station, ttype = request.args.get("q", ""), request.args.get("station", ""), request.args.get("type", "")
    if request.args.get("from") and request.args.get("to"):
        return redirect(url_for("views.search", **{"from": request.args["from"], "to": request.args["to"]}))
    if station:
        from ..services.suggest import resolve_station

        st = resolve_station(station)
        station = st.code if st else station
    sort, direction = request.args.get("sort", "number"), request.args.get("dir", "asc")
    monitored = {"1": True, "0": False}.get(request.args.get("monitored", ""))
    rows, total = train_svc.search_trains(q, station, ttype, monitored, sort, direction, _page(), per_page)
    status = train_svc.latest_status_for([t.id for t in rows])
    return render_template(
        "pages/trains.html", rows=rows, total=total, page=_page(), pages=_pages(total, per_page), status=status,
        types=train_svc.train_types(),
        automated=bool(manager.automated_sources("train_status")),
    )


# ------------------------------------------------------------------ 3. train details
@views_bp.route("/trains/<number>")
@public_page
def train_detail(number):
    train = db.session.query(Train).filter_by(number=number).first()
    if not train:
        abort(404)
    schedule = manager.fetch("train_schedule", train_number=number)
    live = None
    if request.args.get("check") == "1":
        live = train_svc.refresh_train_status(number, use_cache=True)
    history = (
        db.session.query(TrainLiveStatus).filter_by(train_number=number)
        .order_by(TrainLiveStatus.id.desc()).limit(30).all()
    )
    disruptions, _ = disruption_svc.query(q=number, date_from=today_ist() - timedelta(days=30), per_page=20)
    names = train_svc.station_name_map({h.last_reported_station_code for h in history})
    return render_template("pages/train_detail.html", train=train, schedule=schedule, live=live, history=history,
                           disruptions=disruptions, names=names, today=today_ist())


@views_bp.route("/trains/<number>/manual-status", methods=["POST"])
@login_required
def train_manual_status(number):
    jd = _date_arg("journey_date", today_ist())
    train_svc.record_manual_status(number, jd, request.form, _manual_source_id())
    flash("Manual status recorded (labelled MANUAL).", "success")
    return redirect(url_for("views.train_detail", number=number))


@views_bp.route("/trains/<number>/monitor", methods=["POST"])
@admin_required
def train_toggle_monitor(number):
    train = db.session.query(Train).filter_by(number=number).first_or_404()
    train.is_monitored = not train.is_monitored
    db.session.commit()
    flash(f"Train {number} {'added to' if train.is_monitored else 'removed from'} monitoring.", "info")
    return redirect(request.referrer or url_for("views.train_detail", number=number))


# ------------------------------------------------------------------ 4. station board
@views_bp.route("/stations")
@public_page
def stations():
    per_page = 50
    monitored = {"1": True, "0": False}.get(request.args.get("monitored", ""))
    rows, total = station_svc.search_stations(request.args.get("q", ""), monitored, _page(), per_page)
    return render_template("pages/stations.html", rows=rows, total=total, page=_page(), pages=_pages(total, per_page))


@views_bp.route("/stations/<code>")
@public_page
def station_board(code):
    station = db.session.query(Station).filter_by(code=code.upper()).first_or_404()
    hours = min(max(int(request.args.get("hours", 2) or 2), 1), 8)
    board = station_svc.station_board(station.code, window_hours=hours)
    return render_template("pages/station_board.html", station=station, board=board, hours=hours)


@views_bp.route("/stations/<code>/monitor", methods=["POST"])
@admin_required
def station_toggle_monitor(code):
    st = db.session.query(Station).filter_by(code=code.upper()).first_or_404()
    st.is_monitored = not st.is_monitored
    db.session.commit()
    flash(f"Station {st.code} {'added to' if st.is_monitored else 'removed from'} monitoring.", "info")
    return redirect(request.referrer or url_for("views.station_board", code=st.code))


# ------------------------------------------------------------------ 5. PNR enquiry
@views_bp.route("/pnr", methods=["GET", "POST"])
@login_required
def pnr_enquiry():
    result, last4, error = None, None, None
    if request.method == "POST":
        raw = request.form.get("pnr", "")
        try:
            result = pnr_svc.enquire(raw, g.user)
            last4 = raw.strip()[-4:]
            if request.form.get("save") == "1":
                p = pnr_svc.save_pnr(
                    g.user, raw, label=request.form.get("label"),
                    journey_date=_date_arg("journey_date"), train_number=request.form.get("train_number"),
                    boarding_station_code=request.form.get("boarding_station_code"),
                )
                if result["status"] == "ok":
                    pnr_svc.apply_status(p, result["data"], source_id=result.get("source_id"),
                                         data_kind="cached" if result.get("cached") else result.get("data_kind") or "live")
                flash("PNR saved for monitoring.", "success")
                return redirect(url_for("views.pnr_detail", pnr_id=p.id))
        except pnr_svc.PnrError as exc:
            error = str(exc)
    return render_template("pages/pnr_enquiry.html", result=result, last4=last4, error=error)


# ------------------------------------------------------------------ 6. saved PNRs
def _own_pnr(pnr_id: int) -> Pnr:
    p = db.session.get(Pnr, pnr_id)
    if p is None or (p.user_id != g.user.id and g.user.role != "admin"):
        abort(404)
    return p


@views_bp.route("/pnrs")
@login_required
def pnr_list():
    rows = db.session.query(Pnr).filter_by(user_id=g.user.id).order_by(Pnr.journey_date.desc(), Pnr.id.desc()).all()
    return render_template("pages/pnr_list.html", rows=rows, automated=bool(manager.automated_sources("pnr_status")))


@views_bp.route("/pnrs/<int:pnr_id>")
@login_required
def pnr_detail(pnr_id):
    p = _own_pnr(pnr_id)
    manual = manager.manual_links("pnr_status", pnr_number="") if not manager.automated_sources("pnr_status") else []
    return render_template("pages/pnr_detail.html", p=p, history=pnr_svc.history_rows(p),
                           progress=pnr_svc.passenger_progress, manual=manual)


@views_bp.route("/pnrs/<int:pnr_id>/check", methods=["POST"])
@login_required
def pnr_check(pnr_id):
    p = _own_pnr(pnr_id)
    res = pnr_svc.check_saved_pnr(p, use_cache=False)
    if res["status"] == "ok":
        flash("Status refreshed.", "success")
    elif res["status"] == "manual_required":
        flash("No automated PNR source is permitted. Check the official site and record the status below.", "warning")
    else:
        flash(f"Check failed: {res.get('error')}", "danger")
    return redirect(url_for("views.pnr_detail", pnr_id=p.id))


@views_bp.route("/pnrs/<int:pnr_id>/manual", methods=["POST"])
@login_required
def pnr_manual(pnr_id):
    p = _own_pnr(pnr_id)
    try:
        hist = pnr_svc.record_manual(p, request.form)
        flash("Status recorded" + (f" — change detected: {hist.change_summary}" if hist.changed else "."), "success")
    except pnr_svc.PnrError as exc:
        flash(str(exc), "danger")
    return redirect(url_for("views.pnr_detail", pnr_id=p.id))


@views_bp.route("/pnrs/<int:pnr_id>/settings", methods=["POST"])
@login_required
def pnr_settings(pnr_id):
    p = _own_pnr(pnr_id)
    p.monitoring_enabled = request.form.get("monitoring_enabled") == "1"
    p.label = request.form.get("label") or None
    iv = request.form.get("check_interval_minutes", "").strip()
    p.check_interval_minutes = max(int(iv), current_app.config["PNR_MIN_CHECK_INTERVAL_MINUTES"]) if iv.isdigit() else None
    jd = _date_arg("journey_date")
    if jd:
        p.journey_date = jd
    if request.form.get("train_number"):
        p.train_number = request.form["train_number"].strip()
    if request.form.get("boarding_station_code"):
        p.boarding_station_code = request.form["boarding_station_code"].strip().upper()
    p.scheduled_departure_at = pnr_svc.derive_departure(p)
    p.next_check_at = pnr_svc.next_pnr_check(p) if p.monitoring_enabled else None
    from ..services.chart import next_chart_check

    p.next_chart_check_at = next_chart_check(p)
    db.session.commit()
    flash("Monitoring settings saved.", "success")
    return redirect(url_for("views.pnr_detail", pnr_id=p.id))


@views_bp.route("/pnrs/<int:pnr_id>/delete", methods=["POST"])
@login_required
def pnr_delete(pnr_id):
    pnr_svc.delete_pnr(_own_pnr(pnr_id))
    flash("PNR and all its stored history were permanently deleted.", "info")
    return redirect(url_for("views.pnr_list"))


# ------------------------------------------------------------------ 7. chart monitor
@views_bp.route("/charts")
@login_required
def charts():
    rows = (
        db.session.query(Pnr).filter(Pnr.user_id == g.user.id)
        .order_by(Pnr.scheduled_departure_at.is_(None), Pnr.scheduled_departure_at, Pnr.journey_date).all()
    )
    events = (
        db.session.query(ChartStatusHistory, Pnr).join(Pnr, Pnr.id == ChartStatusHistory.pnr_id)
        .filter(Pnr.user_id == g.user.id).order_by(ChartStatusHistory.observed_at.desc()).limit(100).all()
    )
    return render_template("pages/charts.html", rows=rows, events=events,
                           automated=bool(manager.automated_sources("chart_status")), now=utcnow())


# ------------------------------------------------------------------ 8. disruptions
@views_bp.route("/disruptions")
@views_bp.route("/disruptions/<dtype>")
@public_page
def disruptions(dtype="cancelled"):
    if dtype not in DISRUPTION_TYPES and dtype != "all":
        abort(404)
    per_page = 100
    d_from = _date_arg("from", today_ist())
    d_to = _date_arg("to", d_from)
    rows, total = disruption_svc.query(None if dtype == "all" else dtype, d_from, d_to, request.args.get("q", ""), _page(), per_page)
    cap = {"cancelled": "cancelled_trains", "rescheduled": "rescheduled_trains", "diverted": "diverted_trains"}.get(dtype, "cancelled_trains")
    manual = [] if manager.automated_sources(cap) else manager.manual_links(cap, on_date=d_from)
    return render_template("pages/disruptions.html", rows=rows, total=total, dtype=dtype, types=DISRUPTION_TYPES,
                           d_from=d_from, d_to=d_to, page=_page(), pages=_pages(total, per_page), manual=manual)


@views_bp.route("/disruptions/manual", methods=["POST"])
@admin_required
def disruption_manual():
    jd = _date_arg("journey_date")
    try:
        if not jd:
            raise ValueError("journey date required")
        disruption_svc.upsert(request.form, request.form.get("disruption_type", ""), jd, _manual_source_id(), "manual")
        db.session.commit()
        flash("Disruption recorded (labelled MANUAL).", "success")
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
    return redirect(url_for("views.disruptions", dtype=request.form.get("disruption_type") or "cancelled",
                            **({"from": jd.isoformat()} if jd else {})))


@views_bp.route("/disruptions/import", methods=["POST"])
@admin_required
def disruption_import():
    f = request.files.get("file")
    if not f:
        flash("Choose a CSV file", "danger")
    else:
        count, errors = disruption_svc.import_csv(f.read().decode("utf-8-sig", errors="replace"))
        flash(f"Imported {count} rows." + (f" {len(errors)} errors: " + "; ".join(errors[:5]) if errors else ""),
              "warning" if errors else "success")
    return redirect(url_for("views.disruptions", dtype="all"))


# ------------------------------------------------------------------ 9. delayed trains
@views_bp.route("/delayed")
@public_page
def delayed():
    threshold = int(request.args.get("min", current_app.config["DELAY_THRESHOLD_MINUTES"]) or 0)
    jd = _date_arg("date", today_ist())
    rows = train_svc.delayed_trains(threshold, jd)
    names = dict(db.session.query(Train.number, Train.name).filter(Train.number.in_([r.train_number for r in rows])).all()) if rows else {}
    return render_template("pages/delayed.html", rows=rows, threshold=threshold, jd=jd, names=names,
                           automated=bool(manager.automated_sources("train_status")))


# ------------------------------------------------------------------ 10. daily reports
@views_bp.route("/reports")
@public_page
def reports():
    d = _date_arg("date", today_ist())
    stored = db.session.query(DailyReport).filter_by(report_date=d).first()
    live_summary = report_svc.build_daily_summary(d)
    recent = db.session.query(DailyReport).order_by(DailyReport.report_date.desc()).limit(30).all()
    return render_template("pages/reports.html", d=d, summary=live_summary, stored=stored, recent=recent)


@views_bp.route("/reports/generate", methods=["POST"])
@admin_required
def reports_generate():
    d = _date_arg("date", today_ist())
    report_svc.save_daily_summary(d)
    flash(f"Daily report for {d} saved.", "success")
    return redirect(url_for("views.reports", date=d.isoformat()))


# ------------------------------------------------------------------ 11. source health
@views_bp.route("/sources")
@public_page
def sources():
    recent = (
        db.session.query(SourceHealth).order_by(SourceHealth.checked_at.desc()).limit(100).all()
    )
    return render_template("pages/sources.html", summary=health_summary(24), recent=recent)


@views_bp.route("/sources/probe", methods=["POST"])
@admin_required
def sources_probe():
    from ..services.health import probe_all

    probe_all()
    flash("Health probes completed.", "info")
    return redirect(url_for("views.sources"))


# ------------------------------------------------------------------ 12. settings & logs
@views_bp.route("/settings")
@admin_required
def settings():
    level = request.args.get("level", "")
    logs_q = db.session.query(SystemLog)
    if level:
        logs_q = logs_q.filter(SystemLog.level == level)
    comp = request.args.get("component", "")
    if comp:
        logs_q = logs_q.filter(SystemLog.component.like(f"{comp}%"))
    logs = logs_q.order_by(SystemLog.id.desc()).limit(200).all()
    jobs = db.session.query(MonitoringJob).order_by(MonitoringJob.job_key).all()
    srcs = db.session.query(ApiSource).order_by(ApiSource.priority).all()
    counts = dict(db.session.query(SystemLog.level, func.count(SystemLog.id)).group_by(SystemLog.level).all())
    return render_template("pages/settings.html", logs=logs, jobs=jobs, sources=srcs, level=level, component=comp, counts=counts)


@views_bp.route("/settings/jobs/<key>", methods=["POST"])
@admin_required
def settings_job(key):
    job = db.session.query(MonitoringJob).filter_by(job_key=key).first_or_404()
    action = request.form.get("action")
    if action == "pause":
        job.enabled = False
    elif action == "resume":
        job.enabled = True
    elif action == "run":
        job.run_requested_at = utcnow()
    elif action == "save":
        if job.trigger_type == "interval":
            iv = request.form.get("interval_seconds", "")
            if iv.isdigit():
                job.interval_seconds = max(60, int(iv))
        else:
            cron = request.form.get("cron", "").strip()
            from apscheduler.triggers.cron import CronTrigger

            try:
                CronTrigger.from_crontab(cron)
                job.cron = cron
            except ValueError:
                flash("Invalid cron expression", "danger")
    db.session.commit()
    flash(f"Job '{job.name}': {action} applied (the worker picks this up within ~15 s).", "info")
    return redirect(url_for("views.settings"))


@views_bp.route("/settings/sources/<key>", methods=["POST"])
@admin_required
def settings_source(key):
    src = db.session.query(ApiSource).filter_by(key=key).first_or_404()
    src.enabled = request.form.get("enabled") == "1"
    for field, lo in (("priority", 0), ("rate_limit_per_minute", 1)):
        val = request.form.get(field, "")
        if val.isdigit():
            setattr(src, field, max(lo, int(val)))
    access = request.form.get("automated_access")
    if access in ("permitted", "not_permitted", "unverified", "not_applicable"):
        src.automated_access = access
    db.session.commit()
    flash(f"Source '{src.name}' updated.", "info")
    return redirect(url_for("views.settings"))
