from datetime import date, datetime, timedelta

import pytest

from railmon.extensions import db
from railmon.models import ChartStatusHistory, Pnr, PnrPassenger, PnrStatusHistory
from railmon.services import chart as chart_svc
from railmon.services import pnr as pnr_svc
from railmon.timeutil import utcnow

from .conftest import add_fake_source, make_user


def _data(p1, p2=None, chart=None):
    pax = [{"serial": 1, "booking_status": "WL/23", "current_status": p1}]
    if p2:
        pax.append({"serial": 2, "booking_status": "WL/24", "current_status": p2})
    return {"train_number": "12002", "journey_date": "2026-11-05", "chart_status": chart, "passengers": pax}


def test_save_encrypts_and_dedupes(app, user):
    p = pnr_svc.save_pnr(user, "1234567890")
    assert p.pnr_last4 == "7890" and "1234567890" not in p.pnr_encrypted
    assert pnr_svc.save_pnr(user, "123 456 7890").id == p.id
    with pytest.raises(pnr_svc.PnrError):
        pnr_svc.save_pnr(user, "12345")


def test_saved_limit(app, user):
    app.config["PNR_MAX_SAVED_PER_USER"] = 2
    pnr_svc.save_pnr(user, "1000000001")
    pnr_svc.save_pnr(user, "1000000002")
    with pytest.raises(pnr_svc.PnrError):
        pnr_svc.save_pnr(user, "1000000003")


def test_history_and_change_detection(app, user):
    p = pnr_svc.save_pnr(user, "1234567890")
    h1 = pnr_svc.apply_status(p, _data("WL/10", "WL/11"), source_id=None, data_kind="live")
    h2 = pnr_svc.apply_status(p, _data("WL/10", "WL/11"), source_id=None, data_kind="live")
    h3 = pnr_svc.apply_status(p, _data("CNF/S5/32", "RAC/5"), source_id=None, data_kind="live")
    assert (h1.changed, h2.changed, h3.changed) == (False, False, True)
    assert "P1: WL/10 → CNF/S5/32" in h3.change_summary and "P2: WL/11 → RAC/5" in h3.change_summary
    pax = {x.serial: x for x in p.passengers}
    assert pax[1].current_code == "CNF" and pax[1].coach == "S5" and pax[1].booking_code == "WL"
    assert pnr_svc.passenger_progress(pax[1]).startswith("Confirmed")
    assert pnr_svc.passenger_progress(pax[2]).startswith("Moved to RAC")
    assert db.session.query(PnrStatusHistory).filter_by(pnr_id=p.id).count() == 3
    # snapshots are encrypted at rest
    assert "CNF/S5/32" not in h3.snapshot_encrypted


def test_chart_status_never_inferred(app, user):
    p = pnr_svc.save_pnr(user, "1234567890", journey_date=date.today() - timedelta(days=1))
    # departure has passed, but no source said anything about the chart
    pnr_svc.apply_status(p, _data("WL/1"), source_id=None, data_kind="live")
    assert p.chart_status == "unknown"
    assert db.session.query(ChartStatusHistory).count() == 0


def test_chart_changes_are_timestamped(app, user):
    p = pnr_svc.save_pnr(user, "1234567890")
    pnr_svc.apply_status(p, _data("WL/1", chart="Chart Not Prepared"), source_id=None, data_kind="live")
    pnr_svc.apply_status(p, _data("WL/1", chart=None), source_id=None, data_kind="live")  # unknown does not overwrite
    assert p.chart_status == "not_prepared"
    pnr_svc.apply_status(p, _data("CNF/B1/5", chart="Chart Prepared"), source_id=None, data_kind="live")
    events = db.session.query(ChartStatusHistory).filter_by(pnr_id=p.id).order_by(ChartStatusHistory.id).all()
    assert [(e.previous_status, e.chart_status) for e in events] == [("unknown", "not_prepared"), ("not_prepared", "prepared")]
    assert all(e.observed_at for e in events) and p.chart_status_changed_at == events[-1].observed_at


def test_manual_entry(app, user):
    p = pnr_svc.save_pnr(user, "1234567890")
    pnr_svc.record_manual(p, {"p1_booking": "WL/5", "p1_current": "WL/2", "chart_status": ""})
    h = pnr_svc.record_manual(p, {"p1_booking": "WL/5", "p1_current": "RAC/3", "chart_status": "prepared"})
    assert h.data_kind == "manual" and h.changed and p.chart_status == "prepared"
    with pytest.raises(pnr_svc.PnrError):
        pnr_svc.record_manual(p, {})


def test_check_saved_pnr_with_permitted_source(app, user):
    add_fake_source(capabilities=("pnr_status", "chart_status"),
                    data={"pnr_status": _data("RAC/4", chart="Chart Not Prepared"), "chart_status": {"chart_status": "Chart Prepared"}})
    p = pnr_svc.save_pnr(user, "1234567890")
    res = pnr_svc.check_saved_pnr(p)
    assert res["status"] == "ok" and p.last_check_result == "ok" and p.chart_status == "not_prepared"
    assert p.next_check_at > utcnow()
    chart_svc.check_chart(p)
    assert p.chart_status == "prepared" and p.next_chart_check_at is None


def test_check_saved_pnr_manual_only(app, user):
    p = pnr_svc.save_pnr(user, "1234567890")
    res = pnr_svc.check_saved_pnr(p)
    assert res["status"] == "manual_required" and p.last_check_result == "manual_required"
    assert p.passengers == []


def test_adaptive_pnr_interval(app, user):
    now = datetime(2026, 11, 1, 0, 0)
    p = Pnr(monitoring_enabled=True, check_interval_minutes=None)
    p.scheduled_departure_at = now + timedelta(days=10)
    assert pnr_svc.next_pnr_check(p, now) == now + timedelta(minutes=720)
    p.scheduled_departure_at = now + timedelta(hours=5)
    assert pnr_svc.next_pnr_check(p, now) == now + timedelta(minutes=30)
    p.scheduled_departure_at = now - timedelta(hours=7)
    assert pnr_svc.next_pnr_check(p, now) is None
    p.check_interval_minutes = 5  # below floor
    assert pnr_svc.next_pnr_check(p, now) == now + timedelta(minutes=app.config["PNR_MIN_CHECK_INTERVAL_MINUTES"])


def test_adaptive_chart_interval(app):
    now = datetime(2026, 11, 1, 0, 0)
    p = Pnr(monitoring_enabled=True, chart_status="unknown")
    p.scheduled_departure_at = now + timedelta(hours=48)
    assert chart_svc.next_chart_check(p, now) == p.scheduled_departure_at - timedelta(hours=24)
    p.scheduled_departure_at = now + timedelta(hours=10)
    assert chart_svc.next_chart_check(p, now) == now + timedelta(minutes=120)
    p.scheduled_departure_at = now + timedelta(hours=3)
    assert chart_svc.next_chart_check(p, now) == now + timedelta(minutes=30)
    p.scheduled_departure_at = now + timedelta(hours=1)
    assert chart_svc.next_chart_check(p, now) == now + timedelta(minutes=15)
    p.scheduled_departure_at = now - timedelta(hours=3)
    assert chart_svc.next_chart_check(p, now) is None
    p.scheduled_departure_at, p.chart_status = now + timedelta(hours=1), "prepared"
    assert chart_svc.next_chart_check(p, now) is None


def test_delete_cascades(app, user):
    p = pnr_svc.save_pnr(user, "1234567890")
    pnr_svc.apply_status(p, _data("WL/1", chart="Chart Not Prepared"), source_id=None, data_kind="live")
    pnr_svc.delete_pnr(p)
    assert db.session.query(Pnr).count() == 0
    assert db.session.query(PnrPassenger).count() == 0
    assert db.session.query(PnrStatusHistory).count() == 0
    assert db.session.query(ChartStatusHistory).count() == 0


def test_purge_expired(app, user):
    old = pnr_svc.save_pnr(user, "1000000001", journey_date=date.today() - timedelta(days=30))
    pnr_svc.save_pnr(user, "1000000002", journey_date=date.today() + timedelta(days=3))
    assert pnr_svc.purge_expired() == 1
    assert db.session.get(Pnr, old.id) is None


def test_enquiry_rate_limited_per_user(app, user):
    app.config["PNR_ENQUIRIES_PER_USER_PER_HOUR"] = 2
    assert pnr_svc.enquire("1234567890", user)["status"] == "manual_required"
    assert pnr_svc.enquire("1234567890", user)["status"] == "manual_required"
    assert pnr_svc.enquire("1234567890", user)["status"] == "rate_limited"
    other = make_user("bob")
    assert pnr_svc.enquire("1234567890", other)["status"] == "manual_required"
