from datetime import date

from railmon.extensions import db
from railmon.importers.datameet import import_dir
from railmon.models import Station, Train, TrainRoute, TrainSchedule
from railmon.services import disruptions as disruption_svc
from railmon.services import pnr as pnr_svc
from railmon.services import trains as train_svc
from railmon.services.sources import manager


def test_import_and_idempotent(app, datameet_dir):
    s1 = import_dir(datameet_dir, echo=lambda *_: None)
    assert s1["trains"] == 1 and s1["routes"] == 5
    s2 = import_dir(datameet_dir, echo=lambda *_: None)
    assert s2["schedules"] == 0 and s2["routes"] == 0
    t = db.session.query(Train).filter_by(number="12002").one()
    assert t.classes == ["CC"] and t.duration_minutes == 520 and t.running_days is None
    # stations referenced only by schedules/trains are created as inferred
    assert db.session.query(Station).filter_by(code="RKMP").one().data_source == "datameet:inferred"
    routes = db.session.query(TrainRoute).order_by(TrainRoute.stop_sequence).all()
    assert [r.station_code for r in routes] == ["NDLS", "MTJ", "AGC", "BPL", "RKMP"]
    assert [r.is_halt for r in routes] == [True, False, True, True, True]  # MTJ arr == dep -> pass-through


def test_schedule_change_creates_new_version(app, datameet_dir):
    import json

    import_dir(datameet_dir, echo=lambda *_: None)
    sched = json.loads((datameet_dir / "schedules.json").read_text())
    sched[2]["departure"] = "08:00:00"
    (datameet_dir / "schedules.json").write_text(json.dumps(sched))
    import_dir(datameet_dir, echo=lambda *_: None)
    versions = db.session.query(TrainSchedule).all()
    assert len(versions) == 2 and sum(v.is_current for v in versions) == 1


def test_search_and_route(app, datameet_dir):
    import_dir(datameet_dir, echo=lambda *_: None)
    rows, total = train_svc.search_trains("shatabdi")
    assert total == 1
    rows, total = train_svc.search_trains(station="BPL")
    assert total == 1
    between = train_svc.trains_between("NDLS", "BPL")
    assert len(between) == 1 and between[0]["departure"] == "06:00:00"
    assert train_svc.trains_between("BPL", "NDLS") == []
    assert train_svc.trains_between("NDLS", "MTJ") == []  # pass-through point is not a halt


def test_static_schedule_is_labelled_static(app, datameet_dir):
    import_dir(datameet_dir, echo=lambda *_: None)
    res = manager.fetch("train_schedule", train_number="12002")
    assert res["status"] == "ok" and res["data_kind"] == "static" and len(res["data"]["stops"]) == 5
    board = manager.fetch("station_schedule", station_code="AGC", window_hours=24)
    assert board["data_kind"] == "static" and "not live" in board["data"]["note"]
    assert board["data"]["entries"][0]["train_number"] == "12002"


def test_pnr_departure_derived_from_timetable(app, datameet_dir, user):
    import_dir(datameet_dir, echo=lambda *_: None)
    p = pnr_svc.save_pnr(user, "1234567890", journey_date=date(2026, 11, 5), train_number="12002", boarding_station_code="AGC")
    # 07:55 IST == 02:25 UTC
    assert p.scheduled_departure_at.isoformat() == "2026-11-05T02:25:00"
    assert p.chart_status == "unknown"


def test_csv_import(app):
    csv_text = (
        "date,train_number,type,train_name,from_station_code,to_station_code,affected_from_code,affected_to_code,"
        "rescheduled_departure,rescheduled_by_minutes,diverted_route,reason\n"
        "2026-11-02,12002,cancelled,Shatabdi,NDLS,RKMP,,,,,,Fog\n"
        "2026-11-02,12951,rescheduled,Rajdhani,BCT,NDLS,,,2026-11-02T19:40:00,180,,Late rake\n"
        "bad-date,1,cancelled,,,,,,,,,\n"
        "2026-11-02,2,flooded,,,,,,,,,\n"
    )
    count, errors = disruption_svc.import_csv(csv_text)
    assert count == 2 and len(errors) == 2
    rows, total = disruption_svc.query(date_from=date(2026, 11, 2), date_to=date(2026, 11, 2))
    assert total == 2 and all(r.data_kind == "manual" for r in rows)
    res = [r for r in rows if r.disruption_type == "rescheduled"][0]
    assert res.rescheduled_by_minutes == 180 and res.rescheduled_departure.isoformat() == "2026-11-02T14:10:00"
