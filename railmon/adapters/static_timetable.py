"""Static timetable adapter backed by the local database (imported open data).

Data is clearly marked ``data_kind = "static"``: it is a published/open
timetable, *not* live running information.
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .base import NOT_FOUND, OK, AdapterResponse, BaseAdapter, HealthResult

IST = ZoneInfo("Asia/Kolkata")


def _minutes(t: str | None) -> int | None:
    if not t or t == "None":
        return None
    try:
        h, m, *_ = t.split(":")
        return int(h) * 60 + int(m)
    except ValueError:
        return None


class StaticTimetableAdapter(BaseAdapter):
    adapter_name = "static_timetable"
    capabilities = {"train_schedule", "station_schedule"}
    local_only = True

    def get_train_schedule(self, train_number):
        from ..models import Train, TrainRoute, TrainSchedule
        from ..extensions import db

        train = db.session.query(Train).filter_by(number=train_number).first()
        if not train:
            return AdapterResponse(status=NOT_FOUND, data_kind="static", network=False)
        sched = db.session.query(TrainSchedule).filter_by(train_id=train.id, is_current=True).first()
        stops = []
        if sched:
            rows = (
                db.session.query(TrainRoute)
                .filter_by(schedule_id=sched.id)
                .order_by(TrainRoute.stop_sequence)
                .all()
            )
            stops = [
                {
                    "seq": r.stop_sequence,
                    "station_code": r.station_code,
                    "station_name": r.station.name if r.station else None,
                    "arrival": r.arrival_time,
                    "departure": r.departure_time,
                    "day": r.day_offset,
                    "is_halt": r.is_halt,
                }
                for r in rows
            ]
        return AdapterResponse(
            status=OK,
            data_kind="static",
            network=False,
            data={
                "train_number": train.number,
                "train_name": train.name,
                "running_days": train.running_days,
                "data_source": sched.data_source if sched else train.data_source,
                "imported_at": sched.imported_at.isoformat() if sched else None,
                "stops": stops,
            },
        )

    def get_station_schedule(self, station_code, window_hours=4):
        """Trains *scheduled* to call at a station within the next ``window_hours`` (IST).

        Running days are often unknown in open data, so this lists every train
        whose timetable has a halt in the window and says so.
        """
        from ..models import Train, TrainRoute, TrainSchedule
        from ..extensions import db

        now = datetime.now(timezone.utc).astimezone(IST)
        start = now.hour * 60 + now.minute
        end = start + window_hours * 60
        rows = (
            db.session.query(TrainRoute, Train)
            .join(TrainSchedule, TrainSchedule.id == TrainRoute.schedule_id)
            .join(Train, Train.id == TrainRoute.train_id)
            .filter(TrainRoute.station_code == station_code, TrainSchedule.is_current.is_(True), TrainRoute.is_halt.is_(True))
            .all()
        )
        entries = []
        for route, train in rows:
            ref = _minutes(route.departure_time) if route.departure_time not in (None, "None") else _minutes(route.arrival_time)
            if ref is None:
                continue
            # handle windows that cross midnight
            if not (start <= ref <= end or start <= ref + 1440 <= end):
                continue
            entries.append(
                {
                    "train_number": train.number,
                    "train_name": train.name,
                    "sch_arr": route.arrival_time if route.arrival_time not in (None, "None") else None,
                    "sch_dep": route.departure_time if route.departure_time not in (None, "None") else None,
                    "running_days": train.running_days,
                    "source_station_code": train.source_station_code,
                    "destination_station_code": train.destination_station_code,
                    "_sort": ref if ref >= start else ref + 1440,
                }
            )
        entries.sort(key=lambda e: e.pop("_sort"))
        return AdapterResponse(
            status=OK,
            data_kind="static",
            network=False,
            data={
                "station_code": station_code,
                "window_hours": window_hours,
                "generated_for": now.isoformat(),
                "note": "Scheduled timetable only - not live. Running days may be unknown; the train may not run today.",
                "entries": entries,
            },
        )

    def health_check(self):
        from ..models import Train
        from ..extensions import db

        count = db.session.query(Train).count()
        if count == 0:
            return HealthResult(status="down", error="No timetable imported. Run: flask railmon import-datameet")
        return HealthResult(status="up", details={"trains": count})
