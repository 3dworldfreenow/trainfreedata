"""Station search and live/static station boards."""
from __future__ import annotations

from sqlalchemy import or_

from ..adapters.base import OK
from ..extensions import db
from ..models import Station, StationBoardSnapshot
from ..security import content_hash
from ..syslog import log_event
from ..timeutil import utcnow
from .sources import manager


def search_stations(q: str = "", monitored: bool | None = None, page: int = 1, per_page: int = 50):
    query = db.session.query(Station)
    q = (q or "").strip()
    if q:
        query = query.filter(or_(Station.code == q.upper(), Station.code.like(f"{q.upper()}%"), Station.name.like(f"%{q}%")))
    if monitored is not None:
        query = query.filter(Station.is_monitored.is_(monitored))
    total = query.count()
    rows = query.order_by(Station.code).offset((max(page, 1) - 1) * per_page).limit(per_page).all()
    return rows, total


def station_board(code: str, window_hours: int = 2, use_cache: bool = True) -> dict:
    """Returns {"live": envelope, "scheduled": envelope, "last_snapshot": StationBoardSnapshot|None}."""
    code = code.upper()
    live = manager.fetch("station_board", use_cache=use_cache, station_code=code, window_hours=window_hours)
    if live["status"] == OK and isinstance(live.get("data"), dict) and not live.get("cached"):
        try:
            store_board(code, window_hours, live)
        except Exception as exc:  # noqa: BLE001
            db.session.rollback()
            log_event("ERROR", "stations", "could not store board", station=code, error=str(exc))
    scheduled = manager.fetch("station_schedule", station_code=code, window_hours=max(window_hours, 4))
    last = (
        db.session.query(StationBoardSnapshot)
        .filter_by(station_code=code)
        .order_by(StationBoardSnapshot.id.desc())
        .first()
    )
    return {"live": live, "scheduled": scheduled, "last_snapshot": last}


def store_board(code: str, window_hours: int, result: dict) -> StationBoardSnapshot:
    entries = result["data"].get("entries") or []
    h = content_hash(entries)
    now = utcnow()
    latest = db.session.query(StationBoardSnapshot).filter_by(station_code=code).order_by(StationBoardSnapshot.id.desc()).first()
    if latest and latest.content_hash == h:
        latest.last_confirmed_at = now
        db.session.commit()
        return latest
    snap = StationBoardSnapshot(
        station_code=code, source_id=result.get("source_id"), data_kind=result.get("data_kind") or "live",
        window_hours=window_hours, entries=entries, content_hash=h, fetched_at=now, last_confirmed_at=now,
    )
    db.session.add(snap)
    db.session.commit()
    return snap
