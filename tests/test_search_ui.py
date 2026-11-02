from railmon.importers.datameet import import_dir
from railmon.services import trains as train_svc
from railmon.services.suggest import display_name, resolve_station, resolve_train, suggest_stations, suggest_trains


def _load(datameet_dir):
    import_dir(datameet_dir, echo=lambda *_: None)


def test_station_suggestions_need_two_chars(app, datameet_dir):
    _load(datameet_dir)
    assert suggest_stations("n") == []
    codes = [s["code"] for s in suggest_stations("nd")]
    assert codes[0] == "NDLS"


def test_station_suggestions_by_name_and_code(app, datameet_dir):
    _load(datameet_dir)
    assert suggest_stations("ndls")[0]["code"] == "NDLS"          # code, case-insensitive
    assert suggest_stations("new d")[0]["name"] == "New Delhi"     # name prefix
    assert "AGC" in [s["code"] for s in suggest_stations("cantt")]  # word inside the name
    assert suggest_stations("bhopal")[0]["trains"] == 1             # halting-train count for ranking


def test_resolve_station_variants(app, datameet_dir):
    _load(datameet_dir)
    for text in ("NDLS", "ndls", "New Delhi", "New Delhi (NDLS)", "new delhi"):
        assert resolve_station(text).code == "NDLS", text
    assert resolve_station("") is None
    assert resolve_station("zzzz-nowhere") is None


def test_train_suggest_and_resolve(app, datameet_dir):
    _load(datameet_dir)
    assert suggest_trains("120")[0]["number"] == "12002"
    assert suggest_trains("shatab")[0]["number"] == "12002"
    assert resolve_train("12002 · Bhopal Shatabdi") == "12002"
    assert resolve_train("shatabdi") == "12002"
    assert resolve_train("99999") is None


def test_display_name():
    assert display_name("MUMBAI DADAR WEST") == "Mumbai Dadar West"
    assert display_name("New Delhi") == "New Delhi"


def test_journey_minutes_and_running_days():
    assert train_svc.journey_minutes("16:40:00", 1, "08:30:00", 2) == 950
    assert train_svc.journey_minutes("06:00:00", 1, "14:10:00", 1) == 490
    assert train_svc.journey_minutes("23:00:00", None, "01:00:00", None) == 120
    assert train_svc.journey_minutes(None, 1, "01:00:00", 1) is None
    assert train_svc.runs_on("1010101", 0) is True and train_svc.runs_on("1010101", 1) is False
    assert train_svc.runs_on(None, 0) is None


def test_suggest_api(client, datameet_dir):
    _load(datameet_dir)
    data = client.get("/api/v1/suggest/stations?q=agra").get_json()
    assert data["items"][0]["code"] == "AGC"
    assert client.get("/api/v1/suggest/stations?q=a").get_json()["items"] == []
    assert client.get("/api/v1/suggest/trains?q=12002").get_json()["items"][0]["name"] == "Bhopal Shatabdi"


def test_search_page_accepts_names_and_codes(client, datameet_dir):
    _load(datameet_dir)
    for frm, to in (("NDLS", "BPL"), ("New Delhi (NDLS)", "Bhopal Jn (BPL)"), ("new delhi", "bhopal")):
        html = client.get("/search", query_string={"from": frm, "to": to, "date": "2026-11-05"}).get_data(as_text=True)
        assert "Bhopal Shatabdi" in html and "8h 10m" in html, (frm, to)
    html = client.get("/search", query_string={"from": "BPL", "to": "NDLS"}).get_data(as_text=True)
    assert "No direct trains" in html
    html = client.get("/search", query_string={"from": "nowhere-x", "to": "NDLS"}).get_data(as_text=True)
    assert "Could not find" in html


def test_go_shortcuts(client, datameet_dir):
    _load(datameet_dir)
    r = client.get("/go/train?q=12002 · Bhopal Shatabdi&check=1")
    assert r.status_code == 302 and "/trains/12002" in r.headers["Location"]
    r = client.get("/go/station?q=Agra Cantt (AGC)")
    assert r.status_code == 302 and r.headers["Location"].endswith("/stations/AGC")
    assert "tab=station" in client.get("/go/station?q=nowhere-x").headers["Location"]


def test_home_page(client):
    html = client.get("/").get_data(as_text=True)
    assert 'data-ac="station"' in html and "Trains between stations" in html
