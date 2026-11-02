"""Cancelled / rescheduled / diverted train reports."""
from __future__ import annotations

import csv
import io
from datetime import date, datetime

from sqlalchemy import or_

from ..adapters.base import OK
from ..extensions import db
from ..models import DISRUPTION_TYPES, ApiSource, Train, TrainDisruption
from ..normalize import parse_dt, to_int
from ..security import content_hash
from ..syslog import log_event
from ..timeutil import utcnow
from .sources import manager

CAPABILITY_TYPE = {"cancelled_trains": "cancelled", "rescheduled_trains": "rescheduled", "diverted_trains": "diverted"}


def refresh_disruptions(on_date: date, use_cache: bool = True) -> dict:
    summary = {}
    for capability, default_type in CAPABILITY_TYPE.items():
        res = manager.fetch(capability, use_cache=use_cache, on_date=on_date)
        summary[capability] = {"status": res["status"], "source": res.get("source"), "stored": 0, "error": res.get("error")}
        if res["status"] != OK or not isinstance(res.get("data"), dict):
            continue
        for item in res["data"].get("trains") or []:
            try:
                with db.session.begin_nested():  # a bad record must not discard the good ones
                    upsert(item, item.get("disruption_type") or default_type, on_date, res.get("source_id"), res.get("data_kind") or "live")
                summary[capability]["stored"] += 1
            except Exception as exc:  # noqa: BLE001
                log_event("ERROR", "disruptions", "bad disruption record", error=str(exc), item=str(item)[:300])
        db.session.commit()
    return summary


def upsert(item: dict, dtype: str, on_date: date, source_id: int | None, data_kind: str) -> TrainDisruption:
    if dtype not in DISRUPTION_TYPES:
        raise ValueError(f"unknown disruption type {dtype}")
    number = str(item.get("train_number") or "").strip()
    if not number:
        raise ValueError("train_number missing")
    fields = dict(
        train_name=item.get("train_name"),
        from_station_code=(item.get("from_station_code") or None),
        to_station_code=(item.get("to_station_code") or None),
        affected_from_code=(item.get("affected_from_code") or None),
        affected_to_code=(item.get("affected_to_code") or None),
        rescheduled_departure=parse_dt(item.get("rescheduled_departure")),
        rescheduled_by_minutes=to_int(item.get("rescheduled_by_minutes")),
        diverted_route=item.get("diverted_route"),
        reason=(item.get("reason") or None),
    )
    h = content_hash({k: str(v) for k, v in fields.items()})
    now = utcnow()
    row = (
        db.session.query(TrainDisruption)
        .filter_by(train_number=number, journey_date=on_date, disruption_type=dtype, source_id=source_id)
        .first()
    )
    if row is None:
        train = db.session.query(Train).filter_by(number=number).first()
        row = TrainDisruption(
            train_id=train.id if train else None, train_number=number, disruption_type=dtype, journey_date=on_date,
            source_id=source_id, data_kind=data_kind, first_seen_at=now, content_hash=h,
        )
        db.session.add(row)
    for k, v in fields.items():
        setattr(row, k, v)
    if not row.train_name and row.train_id:
        row.train_name = db.session.get(Train, row.train_id).name
    row.content_hash, row.last_seen_at, row.data_kind = h, now, data_kind
    db.session.flush()
    return row


def manual_source_id() -> int | None:
    src = db.session.query(ApiSource).filter_by(key="manual").first()
    return src.id if src else None


def import_csv(text: str) -> tuple[int, list[str]]:
    """Import disruptions transcribed from an official notice.

    Columns: date (YYYY-MM-DD), train_number, type, train_name, from_station_code,
    to_station_code, affected_from_code, affected_to_code, rescheduled_departure,
    rescheduled_by_minutes, diverted_route, reason
    """
    reader = csv.DictReader(io.StringIO(text))
    count, errors = 0, []
    sid = manual_source_id()
    for i, row in enumerate(reader, start=2):
        try:
            on = datetime.strptime((row.get("date") or "").strip(), "%Y-%m-%d").date()
            with db.session.begin_nested():
                upsert(row, (row.get("type") or "").strip(), on, sid, "manual")
            count += 1
        except Exception as exc:  # noqa: BLE001
            errors.append(f"line {i}: {exc}")
    db.session.commit()
    return count, errors


def query(dtype: str | None = None, date_from: date | None = None, date_to: date | None = None, q: str = "",
          page: int = 1, per_page: int = 100):
    query_ = db.session.query(TrainDisruption)
    if dtype:
        query_ = query_.filter(TrainDisruption.disruption_type == dtype)
    if date_from:
        query_ = query_.filter(TrainDisruption.journey_date >= date_from)
    if date_to:
        query_ = query_.filter(TrainDisruption.journey_date <= date_to)
    if q:
        code = q.strip().upper()
        query_ = query_.filter(
            or_(
                TrainDisruption.train_number.like(f"{q.strip()}%"),
                TrainDisruption.train_name.like(f"%{q.strip()}%"),
                TrainDisruption.from_station_code == code,
                TrainDisruption.to_station_code == code,
                TrainDisruption.affected_from_code == code,
                TrainDisruption.affected_to_code == code,
            )
        )
    total = query_.count()
    rows = (
        query_.order_by(TrainDisruption.journey_date.desc(), TrainDisruption.train_number)
        .offset((max(page, 1) - 1) * per_page)
        .limit(per_page)
        .all()
    )
    return rows, total
