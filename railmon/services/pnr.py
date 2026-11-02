"""PNR enquiry, saved-PNR monitoring and status history.

Privacy: PNR numbers are encrypted at rest, looked up by keyed hash, and shown
masked (last 4 digits). Passenger names/ages are never stored. Deleting a PNR
cascades to passengers, status history and chart history.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from flask import current_app

from ..adapters.base import OK
from ..extensions import cache, db
from ..models import ApiSource, Pnr, PnrPassenger, PnrStatusHistory, Train, TrainRoute, TrainSchedule, User
from ..normalize import normalize_passengers, normalize_pnr_data
from ..security import content_hash, crypto, normalize_pnr
from ..syslog import log_event
from ..timeutil import ist_to_utc, utcnow
from .chart import apply_chart_status, next_chart_check
from .sources import envelope, manager


class PnrError(ValueError):
    pass


# ----------------------------------------------------------------------------- enquiry
def enquire(raw_pnr: str, user: User) -> dict:
    number = normalize_pnr(raw_pnr)
    if not number:
        raise PnrError("PNR must be exactly 10 digits")
    limit = current_app.config["PNR_ENQUIRIES_PER_USER_PER_HOUR"]
    if not cache.allow(f"user:{user.id}:pnr_enquiry", limit, 3600):
        return envelope(None, "rate_limited", error=f"Limit of {limit} PNR enquiries per hour reached")
    res = manager.fetch("pnr_status", pnr_number=number)
    cache.incr(f"stats:pnr_checks:{utcnow():%Y-%m-%d}")
    if res["status"] == OK and isinstance(res.get("data"), dict):
        res = dict(res, data=normalize_pnr_data(res["data"]) | {"pnr_last4": number[-4:]})
    return res


# ----------------------------------------------------------------------------- saved PNRs
def find_saved(user: User, number: str) -> Pnr | None:
    return db.session.query(Pnr).filter_by(user_id=user.id, pnr_hash=crypto.pnr_hash(number)).first()


def save_pnr(user: User, raw_pnr: str, *, label: str | None = None, journey_date: date | None = None,
             train_number: str | None = None, boarding_station_code: str | None = None,
             travel_class: str | None = None, check_interval_minutes: int | None = None) -> Pnr:
    number = normalize_pnr(raw_pnr)
    if not number:
        raise PnrError("PNR must be exactly 10 digits")
    existing = find_saved(user, number)
    if existing:
        return existing
    count = db.session.query(Pnr).filter_by(user_id=user.id).count()
    if count >= current_app.config["PNR_MAX_SAVED_PER_USER"]:
        raise PnrError("Maximum number of saved PNRs reached; delete old ones first")
    pnr = Pnr(
        user_id=user.id,
        pnr_hash=crypto.pnr_hash(number),
        pnr_encrypted=crypto.encrypt(number),
        pnr_last4=number[-4:],
        label=(label or None),
        journey_date=journey_date,
        train_number=(train_number or "").strip() or None,
        boarding_station_code=(boarding_station_code or "").strip().upper() or None,
        travel_class=(travel_class or "").strip().upper() or None,
        check_interval_minutes=check_interval_minutes,
        monitoring_enabled=True,
    )
    pnr.scheduled_departure_at = derive_departure(pnr)
    pnr.next_check_at = utcnow()
    pnr.next_chart_check_at = next_chart_check(pnr)
    db.session.add(pnr)
    db.session.commit()
    log_event("INFO", "pnr", "PNR saved for monitoring", user=user.username, pnr=f"******{pnr.pnr_last4}")
    return pnr


def derive_departure(pnr: Pnr) -> datetime | None:
    """Scheduled departure from the boarding station using the STATIC timetable.

    Only used to pace checks. Returns None if the train/station isn't known.
    """
    if not (pnr.train_number and pnr.journey_date):
        return None
    train = db.session.query(Train).filter_by(number=pnr.train_number).first()
    if not train:
        return None
    if pnr.boarding_station_code:
        stop = (
            db.session.query(TrainRoute)
            .join(TrainSchedule, TrainSchedule.id == TrainRoute.schedule_id)
            .filter(TrainSchedule.is_current.is_(True), TrainRoute.train_id == train.id,
                    TrainRoute.station_code == pnr.boarding_station_code)
            .first()
        )
        if stop and (stop.departure_time or stop.arrival_time):
            # journey_date on a ticket is the boarding date, so no day offset is applied
            return ist_to_utc(pnr.journey_date, stop.departure_time or stop.arrival_time)
    if train.departure_time and (not pnr.boarding_station_code or pnr.boarding_station_code == train.source_station_code):
        return ist_to_utc(pnr.journey_date, train.departure_time)
    return None


def next_pnr_check(pnr: Pnr, now: datetime | None = None) -> datetime | None:
    now = now or utcnow()
    if not pnr.monitoring_enabled:
        return None
    floor = current_app.config["PNR_MIN_CHECK_INTERVAL_MINUTES"]
    if pnr.check_interval_minutes:
        return now + timedelta(minutes=max(pnr.check_interval_minutes, floor))
    from .chart import departure_reference

    dep = departure_reference(pnr)
    if dep is None:
        step = 360
    else:
        hours = (dep - now).total_seconds() / 3600
        if hours < -6:
            return None  # journey over
        step = 720 if hours > 168 else 360 if hours > 48 else 120 if hours > 12 else 30
    return now + timedelta(minutes=max(step, floor))


def apply_status(pnr: Pnr, data: dict, *, source_id: int | None, data_kind: str, checked_at: datetime | None = None) -> PnrStatusHistory:
    """Apply a normalised PNR status (from a source or manual entry) and snapshot it."""
    checked_at = checked_at or utcnow()
    norm = normalize_pnr_data(data)
    for attr in ("train_number", "train_name", "from_station_code", "to_station_code", "boarding_station_code", "travel_class"):
        if norm.get(attr):
            setattr(pnr, attr, norm[attr])
    if norm.get("journey_date"):
        pnr.journey_date = date.fromisoformat(norm["journey_date"])
    pnr.scheduled_departure_at = derive_departure(pnr) or pnr.scheduled_departure_at

    passengers = norm["passengers"]
    previous = (
        db.session.query(PnrStatusHistory).filter_by(pnr_id=pnr.id).order_by(PnrStatusHistory.id.desc()).first()
    )
    prev_snapshot = crypto.decrypt_json(previous.snapshot_encrypted) if previous else None
    chart_before = pnr.chart_status
    chart_changed = apply_chart_status(pnr, norm["chart_status"], source_id, data_kind, checked_at)

    snapshot = {"passengers": passengers, "chart_status": pnr.chart_status}
    h = content_hash(snapshot)
    summary = describe_changes(prev_snapshot, snapshot) if prev_snapshot else None
    if chart_changed and not summary:
        summary = f"Chart: {chart_before} → {pnr.chart_status}"

    # upsert passengers
    existing = {p.serial: p for p in pnr.passengers}
    for p in passengers:
        row = existing.get(p["serial"]) or PnrPassenger(pnr_id=pnr.id, serial=p["serial"])
        for k in ("booking_status", "booking_code", "current_status", "current_code", "coach", "berth"):
            setattr(row, k, p.get(k))
        if row.id is None:
            db.session.add(row)

    hist = PnrStatusHistory(
        pnr_id=pnr.id, checked_at=checked_at, source_id=source_id, data_kind=data_kind,
        chart_status=pnr.chart_status, snapshot_encrypted=crypto.encrypt_json(snapshot), content_hash=h,
        changed=bool(previous and previous.content_hash != h), change_summary=summary,
    )
    db.session.add(hist)
    pnr.last_checked_at = checked_at
    pnr.last_check_result = "ok"
    pnr.check_count += 1
    db.session.commit()
    if hist.changed:
        log_event("INFO", "pnr", "PNR status changed", pnr=f"******{pnr.pnr_last4}", summary=summary)
    return hist


def describe_changes(prev: dict, cur: dict) -> str | None:
    msgs = []
    prev_p = {p["serial"]: p for p in prev.get("passengers", [])}
    for p in cur.get("passengers", []):
        old = prev_p.get(p["serial"])
        if old and old.get("current_status") != p.get("current_status"):
            msgs.append(f"P{p['serial']}: {old.get('current_status') or '-'} → {p.get('current_status') or '-'}")
        elif not old:
            msgs.append(f"P{p['serial']}: added ({p.get('current_status') or '-'})")
    if prev.get("chart_status") != cur.get("chart_status"):
        msgs.append(f"Chart: {prev.get('chart_status')} → {cur.get('chart_status')}")
    return "; ".join(msgs) or None


def passenger_progress(p: PnrPassenger) -> str:
    """Human summary comparing booking status with current status."""
    b, c = p.booking_code, p.current_code
    if not c:
        return "Current status not reported"
    if b == c and p.booking_status == p.current_status:
        return "Unchanged since booking"
    if c == "CNF" and b != "CNF":
        return f"Confirmed (booked as {b or p.booking_status})"
    if c == "RAC" and b not in ("RAC", "CNF"):
        return f"Moved to RAC (booked as {b or p.booking_status})"
    if c == "CAN":
        return "Cancelled"
    return f"Changed from {p.booking_status or '-'} to {p.current_status}"


def check_saved_pnr(pnr: Pnr, use_cache: bool = True) -> dict:
    number = crypto.decrypt(pnr.pnr_encrypted)
    if not number:
        return {"status": "error", "error": "cannot decrypt PNR"}
    res = manager.fetch("pnr_status", use_cache=use_cache, pnr_number=number)
    cache.incr(f"stats:pnr_checks:{utcnow():%Y-%m-%d}")
    now = utcnow()
    if res["status"] == OK and isinstance(res.get("data"), dict):
        apply_status(pnr, res["data"], source_id=res.get("source_id"),
                     data_kind="cached" if res.get("cached") else (res.get("data_kind") or "live"), checked_at=now)
    else:
        pnr.last_checked_at = now
        pnr.last_check_result = res["status"]
    pnr.next_check_at = next_pnr_check(pnr, now)
    pnr.next_chart_check_at = next_chart_check(pnr, now)
    if pnr.next_check_at is None and pnr.monitoring_enabled and pnr.journey_date and pnr.journey_date < now.date() - timedelta(days=1):
        pnr.monitoring_enabled = False
    db.session.commit()
    return {"status": res["status"], "error": res.get("error"), "manual_enquiry": res.get("manual_enquiry")}


def record_manual(pnr: Pnr, form: dict) -> PnrStatusHistory:
    """Record a status the user read from an official channel (labelled MANUAL)."""
    passengers = []
    for i in range(1, 7):
        b, c = (form.get(f"p{i}_booking") or "").strip(), (form.get(f"p{i}_current") or "").strip()
        if b or c:
            passengers.append({"serial": i, "booking_status": b or None, "current_status": c or None})
    if not passengers:
        raise PnrError("Enter at least one passenger status")
    data = {
        "train_number": form.get("train_number"),
        "journey_date": form.get("journey_date"),
        "boarding_station_code": form.get("boarding_station_code"),
        "travel_class": form.get("travel_class"),
        "chart_status": form.get("chart_status") or None,
        "passengers": normalize_passengers(passengers),
    }
    src = db.session.query(ApiSource).filter_by(key="manual").first()
    hist = apply_status(pnr, data, source_id=src.id if src else None, data_kind="manual")
    pnr.next_chart_check_at = next_chart_check(pnr)
    db.session.commit()
    return hist


def history_rows(pnr: Pnr) -> list[dict]:
    out = []
    for h in pnr.history:
        snap = crypto.decrypt_json(h.snapshot_encrypted) or {}
        out.append({"row": h, "passengers": snap.get("passengers", [])})
    return out


def delete_pnr(pnr: Pnr) -> None:
    last4 = pnr.pnr_last4
    db.session.delete(pnr)
    db.session.commit()
    log_event("INFO", "pnr", "PNR and its history deleted", pnr=f"******{last4}")


def purge_expired() -> int:
    days = current_app.config["PNR_RETENTION_DAYS_AFTER_JOURNEY"]
    cutoff = (utcnow() - timedelta(days=days)).date()
    rows = db.session.query(Pnr).filter(Pnr.journey_date.isnot(None), Pnr.journey_date < cutoff).all()
    for r in rows:
        db.session.delete(r)
    db.session.commit()
    return len(rows)
