"""Autocomplete suggestions and free-text resolution for stations and trains.

Users can type a station code ("NDLS"), a name ("new del"), or pick a
suggestion ("New Delhi (NDLS)"); all resolve to the same station code.
"""
from __future__ import annotations

import re
import time

from sqlalchemy import func, or_

from ..extensions import db
from ..models import Station, Train, TrainRoute, TrainSchedule

_busy_cache: dict = {"at": 0.0, "counts": {}}
_CODE_IN_PARENS = re.compile(r"\(([A-Za-z0-9]{1,10})\)\s*$")
_LEADING_NUMBER = re.compile(r"^\s*(\d{4,6})\b")


def display_name(name: str | None) -> str:
    """'MUMBAI DADAR WEST' -> 'Mumbai Dadar West' (keeps already mixed-case names)."""
    if not name:
        return ""
    return name.title() if name.isupper() else name


def station_busyness() -> dict[str, int]:
    """Number of trains halting at each station (cached in-process for an hour)."""
    if time.time() - _busy_cache["at"] > 3600 or not _busy_cache["counts"]:
        rows = (
            db.session.query(TrainRoute.station_code, func.count(TrainRoute.id))
            .join(TrainSchedule, TrainSchedule.id == TrainRoute.schedule_id)
            .filter(TrainSchedule.is_current.is_(True), TrainRoute.is_halt.is_(True))
            .group_by(TrainRoute.station_code)
            .all()
        )
        _busy_cache.update(at=time.time(), counts=dict(rows))
    return _busy_cache["counts"]


def suggest_stations(q: str, limit: int = 8) -> list[dict]:
    q = (q or "").strip()
    if len(q) < 2:
        return []
    up = q.upper()
    rows = (
        db.session.query(Station)
        .filter(or_(Station.code.like(f"{up}%"), Station.name.like(f"{q}%"), Station.name.like(f"% {q}%")))
        .limit(400)
        .all()
    )
    busy = station_busyness()

    def rank(s: Station):
        name = (s.name or "").upper()
        if s.code == up:
            tier = 0
        elif name == up:
            tier = 1
        elif name.startswith(up):
            tier = 2
        elif s.code.startswith(up):
            tier = 3
        else:
            tier = 4
        return (tier, -busy.get(s.code, 0), s.code)

    rows.sort(key=rank)
    return [
        {"code": s.code, "name": display_name(s.name), "state": s.state, "trains": busy.get(s.code, 0)}
        for s in rows[:limit]
    ]


def suggest_trains(q: str, limit: int = 8) -> list[dict]:
    q = (q or "").strip()
    if len(q) < 2:
        return []
    query = db.session.query(Train)
    query = query.filter(Train.number.like(f"{q}%")) if q.isdigit() else query.filter(Train.name.like(f"%{q}%"))
    rows = query.order_by(Train.number).limit(limit).all()
    return [
        {"number": t.number, "name": display_name(t.name), "from": t.source_station_code, "to": t.destination_station_code}
        for t in rows
    ]


def resolve_station(text: str | None) -> Station | None:
    """Resolve free text ('NDLS', 'New Delhi', 'New Delhi (NDLS)') to a station."""
    text = (text or "").strip()
    if not text:
        return None
    m = _CODE_IN_PARENS.search(text)
    code = (m.group(1) if m else text).upper()
    st = db.session.query(Station).filter(Station.code == code).first()
    if st:
        return st
    exact = db.session.query(Station).filter(func.upper(Station.name) == text.upper()).all()
    if len(exact) == 1:
        return exact[0]
    hits = suggest_stations(text, limit=2)
    return db.session.query(Station).filter_by(code=hits[0]["code"]).first() if hits else None


def resolve_train(text: str | None) -> str | None:
    """Resolve '12951', '12951 · Mumbai Rajdhani' or a unique name fragment to a train number."""
    text = (text or "").strip()
    if not text:
        return None
    m = _LEADING_NUMBER.match(text)
    if m and db.session.query(Train.id).filter_by(number=m.group(1)).first():
        return m.group(1)
    hits = suggest_trains(text, limit=2)
    return hits[0]["number"] if len(hits) == 1 else None
