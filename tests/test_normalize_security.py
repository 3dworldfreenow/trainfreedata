from datetime import datetime

from railmon.normalize import chart_status, normalize_passengers, normalize_pnr_data, parse_dt, split_coach_berth, status_code
from railmon.security import content_hash, crypto, mask_pnr, normalize_pnr


def test_status_codes():
    assert status_code("CNF/S5/32/LB") == "CNF"
    assert status_code("S5/32/LB") == "CNF"
    assert status_code("Confirmed") == "CNF"
    assert status_code("RAC/45") == "RAC"
    assert status_code("WL 23") == "WL"
    assert status_code("GNWL/45") == "GNWL"
    assert status_code("RLWL/12") == "RLWL"
    assert status_code("PQWL 5") == "PQWL"
    assert status_code("TQWL/3") == "TQWL"
    assert status_code("CAN") == "CAN"
    assert status_code("Cancelled") == "CAN"
    assert status_code("NOSB") == "NOSB"
    assert status_code("something odd") == "OTHER"
    assert status_code(None) is None


def test_coach_berth():
    assert split_coach_berth("CNF/S5/32/LB") == ("S5", "32")
    assert split_coach_berth("B2 45") == ("B2", "45")
    assert split_coach_berth("WL/10") == (None, None)


def test_chart_status_is_explicit_only():
    assert chart_status("Chart Prepared") == "prepared"
    assert chart_status("CHART NOT PREPARED") == "not_prepared"
    assert chart_status(True) == "prepared"
    assert chart_status(False) == "not_prepared"
    assert chart_status(None) == "unknown"
    assert chart_status("") == "unknown"
    assert chart_status("departure in 2 hours") == "unknown"


def test_passenger_names_dropped():
    out = normalize_passengers([{"serial": 1, "name": "Some Person", "age": 40, "booking_status": "WL/3", "current_status": "CNF/B1/12"}])
    assert "name" not in out[0] and "age" not in out[0]
    assert out[0]["current_code"] == "CNF" and out[0]["coach"] == "B1"
    data = normalize_pnr_data({"journey_date": "05-11-2026", "passengers": [], "chart_status": "Chart Prepared"})
    assert data["journey_date"] == "2026-11-05" and data["chart_status"] == "prepared"


def test_parse_dt_ist_to_utc():
    assert parse_dt("2026-11-05T10:30:00") == datetime(2026, 11, 5, 5, 0)
    assert parse_dt("2026-11-05T05:00:00Z") == datetime(2026, 11, 5, 5, 0)
    assert parse_dt("garbage") is None


def test_crypto(app):
    token = crypto.encrypt("1234567890")
    assert "1234567890" not in token
    assert crypto.decrypt(token) == "1234567890"
    assert crypto.decrypt("not-a-token") is None
    assert crypto.pnr_hash("1234567890") == crypto.pnr_hash("1234567890") != crypto.pnr_hash("1234567891")
    assert crypto.decrypt_json(crypto.encrypt_json({"a": 1})) == {"a": 1}


def test_pnr_helpers():
    assert normalize_pnr("123-456-7890") == "1234567890"
    assert normalize_pnr("12345") is None
    assert normalize_pnr("12345678901") is None
    assert mask_pnr("7890") == "******7890"
    assert content_hash({"a": 1, "b": 2}) == content_hash({"b": 2, "a": 1})
