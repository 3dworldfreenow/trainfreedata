from datetime import date
from unittest import mock

from railmon.adapters.generic_http import GenericHttpAdapter
from railmon.extensions import db
from railmon.models import ApiSource, SourceHealth
from railmon.services.sources import manager

from .conftest import FakeAdapter, add_fake_source

STATUS = {"train_status": {"running_status": "running", "delay_minutes": 25, "last_reported_station_code": "AGC"}}


def test_no_automated_source_returns_manual_links(app):
    res = manager.fetch("train_status", train_number="12002", journey_date=date(2026, 11, 2))
    assert res["status"] == "manual_required"
    assert set(res) >= {"source", "fetched_at", "status", "data", "cached", "error"}
    assert res["manual_enquiry"][0]["url"].startswith("https://enquiry.indianrail.gov.in")
    assert res["data"] is None  # nothing fabricated


def test_pnr_manual_link(app):
    res = manager.fetch("pnr_status", pnr_number="1234567890")
    assert res["status"] == "manual_required"
    assert "PnrEnquiry" in res["manual_enquiry"][0]["url"]
    assert "CAPTCHA" in res["manual_enquiry"][0]["reason"]


def test_ok_result_is_cached_and_deduplicated(app):
    add_fake_source(data=STATUS)
    r1 = manager.fetch("train_status", train_number="12002", journey_date=date(2026, 11, 2))
    r2 = manager.fetch("train_status", train_number="12002", journey_date=date(2026, 11, 2))
    assert r1["status"] == "ok" and r1["cached"] is False and r1["source"] == "fake1"
    assert r2["cached"] is True and r2["data"] == r1["data"]
    assert len(FakeAdapter.calls) == 1
    manager.fetch("train_status", use_cache=False, train_number="12002", journey_date=date(2026, 11, 2))
    assert len(FakeAdapter.calls) == 2


def test_failover_and_exception_isolation(app):
    add_fake_source("broken", priority=1, mode="raise")
    add_fake_source("flaky", priority=2, mode="error")
    add_fake_source("good", priority=3, data=STATUS)
    res = manager.fetch("train_status", train_number="1", journey_date=date(2026, 11, 2))
    assert res["status"] == "ok" and res["source"] == "good"
    flaky = db.session.query(ApiSource).filter_by(key="flaky").one()
    assert flaky.consecutive_failures == 1 and flaky.last_status == "degraded"
    assert db.session.query(SourceHealth).filter_by(source_id=flaky.id, status="down").count() == 1


def test_all_failed_unavailable(app):
    db.session.query(ApiSource).filter_by(key="ntes").update({"enabled": False})
    add_fake_source("flaky", mode="error")
    res = manager.fetch("train_status", train_number="1", journey_date=date(2026, 11, 2))
    assert res["status"] == "unavailable" and "upstream 503" in res["error"]


def test_rate_limit_per_source(app):
    db.session.query(ApiSource).filter_by(key="ntes").update({"enabled": False})
    add_fake_source(data=STATUS, rate=2)
    statuses = [manager.fetch("train_status", use_cache=False, train_number=str(i), journey_date=date(2026, 11, 2))["status"] for i in range(3)]
    assert statuses == ["ok", "ok", "unavailable"]
    assert len(FakeAdapter.calls) == 2


def test_automated_sources_requires_permission(app):
    add_fake_source("unverified", access="unverified", data=STATUS)
    assert manager.automated_sources("train_status") == []
    add_fake_source("ok", access="permitted", data=STATUS)
    assert [s.key for s in manager.automated_sources("train_status")] == ["ok"]


def test_generic_http_refuses_without_permission(app):
    src = {"key": "g", "adapter": "generic_http", "automated_access": "unverified",
           "config": {"endpoints": {"train_status": "https://example.org/{train_number}"}}}
    adapter = GenericHttpAdapter(src, mock.Mock(), app.config)
    resp = adapter.get_train_status("12002")
    assert resp.status == "disabled" and adapter.http.get.call_count == 0


def test_generic_http_permitted(app):
    http = mock.Mock()
    http.get.return_value = mock.Mock(response=mock.Mock(status_code=200, json=lambda: {"status": "ok", "data": {"delay_minutes": 5}}),
                                      error=None, latency_ms=12)
    src = {"key": "g", "adapter": "generic_http", "automated_access": "permitted",
           "config": {"endpoints": {"train_status": "https://example.org/{train_number}?d={journey_date}"}}}
    resp = GenericHttpAdapter(src, http, app.config).get_train_status("12002", date(2026, 11, 2))
    assert resp.status == "ok" and resp.data == {"delay_minutes": 5}
    assert http.get.call_args[0][0] == "https://example.org/12002?d=2026-11-02"


def test_pnr_never_in_cache_keys(app):
    from railmon.extensions import cache

    add_fake_source(capabilities=("pnr_status",), data={"pnr_status": {"passengers": []}})
    manager.fetch("pnr_status", pnr_number="9876543210")
    assert not any("9876543210" in k for k in cache.client.keys("*"))
