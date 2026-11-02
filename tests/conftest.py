import json
from pathlib import Path

import pytest
from werkzeug.security import generate_password_hash

from railmon import create_app
from railmon.adapters import REGISTRY
from railmon.adapters.base import ERROR, OK, AdapterResponse, BaseAdapter
from railmon.config import TestConfig
from railmon.extensions import cache, db
from railmon.models import ApiSource, User


class FakeAdapter(BaseAdapter):
    """Test adapter whose behaviour is driven by its api_sources.config."""

    adapter_name = "fake"
    calls: list = []

    @property
    def capabilities(self):
        return set(self.settings.get("capabilities", []))

    def _respond(self, cap, **kw):
        FakeAdapter.calls.append((self.key, cap, kw))
        mode = self.settings.get("mode", "ok")
        if mode == "raise":
            raise RuntimeError("boom")
        if mode == "error":
            return AdapterResponse(status=ERROR, error="upstream 503", http_status=503)
        return AdapterResponse(status=OK, data=self.settings.get("data", {}).get(cap, {}), http_status=200, latency_ms=5)

    def get_train_status(self, train_number, journey_date=None):
        return self._respond("train_status", train_number=train_number)

    def get_pnr_status(self, pnr_number):
        return self._respond("pnr_status", pnr_number=pnr_number)

    def get_chart_status(self, pnr_number):
        return self._respond("chart_status", pnr_number=pnr_number)

    def get_cancelled_trains(self, on_date):
        return self._respond("cancelled_trains", on_date=on_date)


REGISTRY["fake"] = FakeAdapter


@pytest.fixture()
def app():
    app = create_app(TestConfig)
    with app.app_context():
        cache.client.flushall()
        db.create_all()
        from railmon.cli import sync_sources
        from railmon.scheduler.runner import sync_jobs

        sync_sources(app.config["SOURCES_CONFIG_PATH"])
        sync_jobs(app.config["SCHEDULER_CONFIG_PATH"])
        FakeAdapter.calls.clear()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture()
def client(app):
    return app.test_client()


def make_user(username="alice", role="user", password="correct-horse-1"):
    u = User(username=username, password_hash=generate_password_hash(password), role=role)
    db.session.add(u)
    db.session.commit()
    return u


@pytest.fixture()
def user(app):
    return make_user()


@pytest.fixture()
def admin(app):
    return make_user("root", "admin")


def add_fake_source(key="fake1", priority=5, mode="ok", capabilities=("train_status",), data=None,
                    access="permitted", rate=100):
    src = ApiSource(key=key, name=f"Fake {key}", adapter="fake", enabled=True, priority=priority,
                    automated_access=access, rate_limit_per_minute=rate,
                    config={"mode": mode, "capabilities": list(capabilities), "data": data or {}})
    db.session.add(src)
    db.session.commit()
    return src


def login(client, username="alice", password="correct-horse-1"):
    import re

    html = client.get("/login").get_data(as_text=True)
    token = re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)
    resp = client.post("/login", data={"username": username, "password": password, "csrf_token": token})
    assert resp.status_code == 302, resp.get_data(as_text=True)
    # the session (and CSRF token) is rotated on login
    html = client.get("/stations").get_data(as_text=True)
    return re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)


@pytest.fixture()
def datameet_dir(tmp_path: Path) -> Path:
    """A tiny dataset in the exact datameet file format."""
    stations = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [77.2, 28.6]},
         "properties": {"code": "NDLS", "name": "New Delhi", "state": "Delhi", "zone": "NR", "address": ""}},
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [77.9, 27.2]},
         "properties": {"code": "AGC", "name": "Agra Cantt", "state": "Uttar Pradesh", "zone": "NCR", "address": ""}},
        {"type": "Feature", "geometry": None,
         "properties": {"code": "BPL", "name": "Bhopal Jn", "state": "Madhya Pradesh", "zone": "WCR", "address": ""}},
    ]}
    trains = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": None, "properties": {
            "number": "12002", "name": "Bhopal Shatabdi", "type": "Shtb", "zone": "NR", "from_station_code": "NDLS",
            "from_station_name": "NEW DELHI", "to_station_code": "RKMP", "to_station_name": "RANI KAMLAPATI",
            "departure": "06:00:00", "arrival": "14:40:00", "duration_h": 8, "duration_m": 40, "distance": 702,
            "chair_car": 1, "first_ac": 0, "return_train": "12001"}},
    ]}
    schedules = [
        {"id": 1, "train_number": "12002", "train_name": "Bhopal Shatabdi", "station_code": "NDLS", "station_name": "NEW DELHI", "arrival": "None", "departure": "06:00:00", "day": 1},
        {"id": 2, "train_number": "12002", "train_name": "Bhopal Shatabdi", "station_code": "MTJ", "station_name": "MATHURA JN", "arrival": "07:19:00", "departure": "07:19:00", "day": 1},
        {"id": 3, "train_number": "12002", "train_name": "Bhopal Shatabdi", "station_code": "AGC", "station_name": "AGRA CANTT", "arrival": "07:50:00", "departure": "07:55:00", "day": 1},
        {"id": 4, "train_number": "12002", "train_name": "Bhopal Shatabdi", "station_code": "BPL", "station_name": "BHOPAL JN", "arrival": "14:10:00", "departure": "14:15:00", "day": 1},
        {"id": 5, "train_number": "12002", "train_name": "Bhopal Shatabdi", "station_code": "RKMP", "station_name": "RANI KAMLAPATI", "arrival": "14:40:00", "departure": "None", "day": 1},
    ]
    for name, data in (("stations.json", stations), ("trains.json", trains), ("schedules.json", schedules)):
        (tmp_path / name).write_text(json.dumps(data))
    return tmp_path
