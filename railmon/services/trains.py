"""Train master-data search and running-status monitoring."""
from __future__ import annotations

from datetime import date

from sqlalchemy import and_, func, or_
from sqlalchemy.orm import aliased

from ..adapters.base import OK
from ..extensions import db
from ..models import Station, Train, TrainLiveStatus, TrainRoute, TrainSchedule
from ..normalize import parse_dt, to_int
from ..security import content_hash
from ..syslog import log_event
from ..timeutil import today_ist, utcnow
from .sources import manager

SORTABLE = {"number": Train.number, "name": Train.name, "type": Train.train_type, "source": Train.source_station_code,
            "destination": Train.destination_station_code, "departure": Train.departure_time}


def search_trains(q: str = "", station: str = "", train_type: str = "", monitored: bool | None = None,
                  sort: str = "number", direction: str = "asc", page: int = 1, per_page: int = 50):
    query = db.session.query(Train)
    q = (q or "").strip()
    if q:
        like = f"%{q}%"
        query = query.filter(or_(Train.number.like(f"{q}%"), Train.name.like(like)))
    if station:
        code = station.strip().upper()
        stops = db.session.query(TrainRoute.train_id).filter(TrainRoute.station_code == code)
        query = query.filter(or_(Train.id.in_(stops), Train.source_station_code == code, Train.destination_station_code == code))
    if train_type:
        query = query.filter(Train.train_type == train_type)
    if monitored is not None:
        query = query.filter(Train.is_monitored.is_(monitored))
    col = SORTABLE.get(sort, Train.number)
    query = query.order_by(col.desc() if direction == "desc" else col.asc(), Train.number)
    total = query.count()
    rows = query.offset((max(page, 1) - 1) * per_page).limit(per_page).all()
    return rows, total


def trains_between(from_code: str, to_code: str, limit: int = 200) -> list[dict]:
    """Route search on the static timetable: trains stopping at A then later at B."""
    a, b = aliased(TrainRoute), aliased(TrainRoute)
    rows = (
        db.session.query(Train, a, b)
        .join(a, a.train_id == Train.id)
        .join(b, and_(b.schedule_id == a.schedule_id, b.stop_sequence > a.stop_sequence))
        .join(TrainSchedule, and_(TrainSchedule.id == a.schedule_id, TrainSchedule.is_current.is_(True)))
        .filter(a.station_code == from_code.upper(), b.station_code == to_code.upper(), a.is_halt.is_(True), b.is_halt.is_(True))
        .order_by(a.departure_time)
        .limit(limit)
        .all()
    )
    return [
        {"train": t, "departure": ra.departure_time, "dep_day": ra.day_offset, "arrival": rb.arrival_time, "arr_day": rb.day_offset}
        for t, ra, rb in rows
    ]


def train_types() -> list[str]:
    return [r[0] for r in db.session.query(Train.train_type).filter(Train.train_type.isnot(None)).distinct().order_by(Train.train_type)]


def latest_status_for(train_ids: list[int]) -> dict[int, TrainLiveStatus]:
    if not train_ids:
        return {}
    sub = (
        db.session.query(TrainLiveStatus.train_id, func.max(TrainLiveStatus.id).label("mid"))
        .filter(TrainLiveStatus.train_id.in_(train_ids))
        .group_by(TrainLiveStatus.train_id)
        .subquery()
    )
    rows = db.session.query(TrainLiveStatus).join(sub, TrainLiveStatus.id == sub.c.mid).all()
    return {r.train_id: r for r in rows}


def latest_statuses(limit: int = 500) -> list[TrainLiveStatus]:
    sub = (
        db.session.query(func.max(TrainLiveStatus.id).label("mid"))
        .group_by(TrainLiveStatus.train_number, TrainLiveStatus.journey_date)
        .subquery()
    )
    return (
        db.session.query(TrainLiveStatus)
        .join(sub, TrainLiveStatus.id == sub.c.mid)
        .order_by(TrainLiveStatus.last_confirmed_at.desc())
        .limit(limit)
        .all()
    )


def delayed_trains(threshold: int, journey_date: date | None = None) -> list[TrainLiveStatus]:
    rows = latest_statuses(5000)
    if journey_date:
        rows = [r for r in rows if r.journey_date == journey_date]
    return sorted((r for r in rows if (r.delay_minutes or 0) >= threshold), key=lambda r: -(r.delay_minutes or 0))


def refresh_train_status(train_number: str, journey_date: date | None = None, use_cache: bool = True) -> dict:
    """Fetch running status via adapters and persist a snapshot if the data changed."""
    journey_date = journey_date or today_ist()
    result = manager.fetch("train_status", use_cache=use_cache, train_number=train_number, journey_date=journey_date)
    if result["status"] == OK and isinstance(result.get("data"), dict) and not result.get("cached"):
        try:
            store_live_status(train_number, journey_date, result)
        except Exception as exc:  # noqa: BLE001
            db.session.rollback()
            log_event("ERROR", "trains", "could not store live status", train=train_number, error=str(exc))
    return result


def store_live_status(train_number: str, journey_date: date, result: dict) -> TrainLiveStatus:
    data = result["data"]
    train = db.session.query(Train).filter_by(number=train_number).first()
    fields = dict(
        running_status=(data.get("running_status") or None),
        current_station_code=data.get("current_station_code"),
        last_reported_station_code=data.get("last_reported_station_code"),
        last_reported_at=parse_dt(data.get("last_reported_at")),
        next_station_code=data.get("next_station_code"),
        delay_minutes=to_int(data.get("delay_minutes")),
        eta=parse_dt(data.get("eta")),
        etd=parse_dt(data.get("etd")),
    )
    h = content_hash({k: str(v) for k, v in fields.items()} | {"stations": data.get("stations")})
    latest = (
        db.session.query(TrainLiveStatus)
        .filter_by(train_number=train_number, journey_date=journey_date)
        .order_by(TrainLiveStatus.id.desc())
        .first()
    )
    now = utcnow()
    if latest and latest.content_hash == h:
        latest.last_confirmed_at = now
        db.session.commit()
        return latest
    row = TrainLiveStatus(
        train_id=train.id if train else None,
        train_number=train_number,
        journey_date=journey_date,
        source_id=result.get("source_id"),
        data_kind=result.get("data_kind") or "live",
        payload=data,
        content_hash=h,
        fetched_at=now,
        last_confirmed_at=now,
        **fields,
    )
    db.session.add(row)
    db.session.commit()
    return row


def record_manual_status(train_number: str, journey_date: date, form: dict, manual_source_id: int | None) -> TrainLiveStatus:
    """Store a running status a user observed on an official channel (labelled MANUAL)."""
    data = {
        "running_status": form.get("running_status") or None,
        "last_reported_station_code": (form.get("last_reported_station_code") or "").upper() or None,
        "current_station_code": (form.get("last_reported_station_code") or "").upper() or None,
        "delay_minutes": to_int(form.get("delay_minutes")),
        "observed_on": form.get("observed_on") or "official channel",
    }
    return store_live_status(
        train_number, journey_date, {"data": data, "data_kind": "manual", "source_id": manual_source_id}
    )


def station_name_map(codes: set[str]) -> dict[str, str]:
    codes = {c for c in codes if c}
    if not codes:
        return {}
    return dict(db.session.query(Station.code, Station.name).filter(Station.code.in_(codes)).all())
