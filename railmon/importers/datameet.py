"""Importer for the datameet Indian Railways open dataset (CC0).

https://github.com/datameet/railways - stations.json, trains.json, schedules.json.
The data is a c. 2016 snapshot. It is imported as STATIC timetable data and is
never presented as live information.
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import requests

from ..extensions import db
from ..models import Station, Train, TrainRoute, TrainSchedule
from ..security import content_hash

log = logging.getLogger(__name__)
BASE = "https://raw.githubusercontent.com/datameet/railways/master/"
FILES = ("stations.json", "trains.json", "schedules.json")
SOURCE = "datameet"


def download(dest: Path, user_agent: str) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        target = dest / name
        if target.exists() and target.stat().st_size > 0:
            continue
        log.info("downloading %s", name)
        with requests.get(BASE + name, stream=True, timeout=120, headers={"User-Agent": user_agent}) as r:
            r.raise_for_status()
            with open(target, "wb") as fh:
                for chunk in r.iter_content(1 << 20):
                    fh.write(chunk)


def _t(v) -> str | None:
    return None if v in (None, "None", "") else str(v)[:8]


def _days_flag(v) -> bool:
    return bool(v) and v not in (0, "0", "false", "False")


def import_dir(path: Path, batch: int = 5000, echo=print) -> dict:
    stations = json.loads((path / "stations.json").read_text())
    trains = json.loads((path / "trains.json").read_text())
    schedules = json.loads((path / "schedules.json").read_text())

    # --- stations -----------------------------------------------------------
    existing = {s.code: s for s in db.session.query(Station).all()}
    added = 0
    for feat in stations["features"]:
        p = feat["properties"]
        code = (p.get("code") or "").strip().upper()
        if not code:
            continue
        coords = (feat.get("geometry") or {}).get("coordinates") or [None, None]
        st = existing.get(code)
        if st is None:
            st = Station(code=code, name=p.get("name") or code, data_source=SOURCE)
            db.session.add(st)
            existing[code] = st
            added += 1
        st.name = (p.get("name") or code)[:128]
        st.state, st.zone, st.address = p.get("state"), p.get("zone"), (p.get("address") or None)
        st.longitude, st.latitude = coords[0], coords[1]
    db.session.commit()
    echo(f"stations: {len(existing)} total ({added} new)")

    # station codes referenced by trains/schedules but missing from stations.json
    def ensure_station(code: str, name: str | None):
        code = (code or "").strip().upper()
        if code and code not in existing:
            st = Station(code=code, name=(name or code).title()[:128], data_source=SOURCE + ":inferred")
            db.session.add(st)
            existing[code] = st
        return code or None

    # --- trains ---------------------------------------------------------------
    tmap = {t.number: t for t in db.session.query(Train).all()}
    for feat in trains["features"]:
        p = feat["properties"]
        num = str(p.get("number") or "").strip()
        if not num:
            continue
        tr = tmap.get(num)
        if tr is None:
            tr = Train(number=num, name=p.get("name") or num, data_source=SOURCE)
            db.session.add(tr)
            tmap[num] = tr
        tr.name = (p.get("name") or num)[:160]
        tr.train_type = p.get("type") or None
        tr.zone = p.get("zone") or None
        tr.source_station_code = ensure_station(p.get("from_station_code"), p.get("from_station_name"))
        tr.destination_station_code = ensure_station(p.get("to_station_code"), p.get("to_station_name"))
        tr.departure_time, tr.arrival_time = _t(p.get("departure")), _t(p.get("arrival"))
        dh, dm = p.get("duration_h"), p.get("duration_m")
        tr.duration_minutes = (int(dh) * 60 + int(dm)) if isinstance(dh, (int, float)) and isinstance(dm, (int, float)) else None
        tr.distance_km = int(p["distance"]) if isinstance(p.get("distance"), (int, float)) else None
        tr.classes = [c for c, k in (("1A", "first_ac"), ("2A", "second_ac"), ("3A", "third_ac"), ("SL", "sleeper"),
                                      ("CC", "chair_car"), ("FC", "first_class")) if _days_flag(p.get(k))] or None
        tr.return_train = p.get("return_train") or None
    db.session.commit()
    echo(f"trains: {len(tmap)}")

    # --- schedules/routes -----------------------------------------------------
    by_train: dict[str, list] = defaultdict(list)
    for s in schedules:
        by_train[str(s.get("train_number") or "").strip()].append(s)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    n_routes = n_sched = 0
    pending = 0
    for num, stops in by_train.items():
        if not num:
            continue
        stops.sort(key=lambda s: s.get("id") or 0)  # ids follow route order in this dataset
        tr = tmap.get(num)
        if tr is None:
            tr = Train(number=num, name=(stops[0].get("train_name") or num)[:160], data_source=SOURCE)
            db.session.add(tr)
            db.session.flush()
            tmap[num] = tr
        if tr.id is None:
            db.session.flush()
        norm = [
            (s.get("station_code"), _t(s.get("arrival")), _t(s.get("departure")), s.get("day"))
            for s in stops
        ]
        h = content_hash(norm)
        current = db.session.query(TrainSchedule).filter_by(train_id=tr.id, is_current=True).first()
        if current and current.schedule_hash == h:
            continue
        if current:
            current.is_current = False
        sched = TrainSchedule(train_id=tr.id, is_current=True, data_source=SOURCE, schedule_hash=h,
                              stop_count=len(stops), imported_at=now, running_days=tr.running_days)
        db.session.add(sched)
        db.session.flush()
        n_sched += 1
        for seq, s in enumerate(stops, start=1):
            code = ensure_station(s.get("station_code"), s.get("station_name"))
            if not code:
                continue
            arr, dep = _t(s.get("arrival")), _t(s.get("departure"))
            day = s.get("day") if isinstance(s.get("day"), int) else None
            db.session.add(TrainRoute(
                schedule_id=sched.id, train_id=tr.id, stop_sequence=seq, station_code=code,
                arrival_time=arr, departure_time=dep, day_offset=day,
                # datameet includes pass-through points where arrival == departure
                is_halt=not (arr and dep and arr == dep) or seq == 1 or seq == len(stops),
            ))
            n_routes += 1
            pending += 1
        if pending >= batch:
            db.session.commit()
            pending = 0
    db.session.commit()
    echo(f"schedules: {n_sched} new/changed, route stops written: {n_routes}")
    return {"stations": len(existing), "trains": len(tmap), "schedules": n_sched, "routes": n_routes}
