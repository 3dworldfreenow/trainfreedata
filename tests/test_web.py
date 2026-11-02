import re
from pathlib import Path

from railmon.extensions import db
from railmon.importers.datameet import import_dir
from railmon.models import MonitoringJob, Pnr

from .conftest import login, make_user

PUBLIC = ["/", "/trains", "/stations", "/delayed", "/disruptions", "/disruptions/all", "/disruptions/diverted",
          "/reports", "/sources", "/login"]


def test_public_pages(client, datameet_dir):
    import_dir(datameet_dir, echo=lambda *_: None)
    for url in PUBLIC + ["/trains/12002", "/trains/12002?check=1", "/stations/AGC", "/trains?from=NDLS&to=BPL"]:
        assert client.get(url).status_code == 200, url


def test_train_check_shows_manual_link_not_fake_data(client, datameet_dir):
    import_dir(datameet_dir, echo=lambda *_: None)
    html = client.get("/trains/12002?check=1").get_data(as_text=True)
    assert "enquiry.indianrail.gov.in/mntes" in html
    assert "STATIC" in html


def test_private_pages_require_login(client):
    for url in ["/pnr", "/pnrs", "/charts", "/settings"]:
        r = client.get(url)
        assert r.status_code == 302 and "/login" in r.headers["Location"]
    assert client.get("/api/v1/pnrs").status_code == 401


def test_login_required_for_public_pages_option(app, client):
    app.config["LOGIN_REQUIRED_FOR_PUBLIC_PAGES"] = True
    assert client.get("/trains").status_code == 302
    assert client.get("/api/v1/health").status_code == 200


def test_csrf_enforced(client, user):
    login(client)
    assert client.post("/pnr", data={"pnr": "1234567890"}).status_code == 400


def test_bad_login(client, user):
    html = client.get("/login").get_data(as_text=True)
    token = re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)
    r = client.post("/login", data={"username": "alice", "password": "wrong", "csrf_token": token})
    assert r.status_code == 200 and "Invalid" in r.get_data(as_text=True)


def test_pnr_flow_masks_number(client, user):
    token = login(client)
    r = client.post("/pnr", data={"pnr": "2345678901", "save": "1", "csrf_token": token})
    assert r.status_code == 302
    detail = r.headers["Location"]
    html = client.get(detail).get_data(as_text=True)
    assert "******8901" in html and "2345678901" not in html and "PnrEnquiry" in html
    r = client.post(detail + "/manual", data={"csrf_token": token, "p1_booking": "WL/9", "p1_current": "CNF/B2/7", "chart_status": "prepared"})
    assert r.status_code == 302
    data = client.get("/api/v1/pnrs").get_json()["items"][0]
    assert data["pnr"] == "******8901" and data["chart_status"] == "prepared" and data["passengers"][0]["coach"] == "B2"
    for url in ["/pnrs", "/charts", detail]:
        assert "2345678901" not in client.get(url).get_data(as_text=True)
    r = client.post(detail + "/delete", data={"csrf_token": token})
    assert r.status_code == 302 and db.session.query(Pnr).count() == 0


def test_users_cannot_see_each_others_pnrs(app, client, user):
    from railmon.services.pnr import save_pnr

    other = make_user("bob")
    p = save_pnr(other, "1111111111")
    login(client)
    assert client.get(f"/pnrs/{p.id}").status_code == 404
    assert client.get(f"/api/v1/pnrs/{p.id}").status_code == 404


def test_settings_admin_only(client, user, admin):
    login(client)
    assert client.get("/settings").status_code == 403
    client.get("/login")
    client2 = client.application.test_client()
    token = login(client2, "root")
    assert client2.get("/settings").status_code == 200
    r = client2.post("/settings/jobs/pnr_checks", data={"csrf_token": token, "action": "pause"})
    assert r.status_code == 302
    assert db.session.query(MonitoringJob).filter_by(job_key="pnr_checks").one().enabled is False
    client2.post("/settings/jobs/pnr_checks", data={"csrf_token": token, "action": "run"})
    assert db.session.query(MonitoringJob).filter_by(job_key="pnr_checks").one().run_requested_at is not None
    r = client2.post("/settings/sources/custom_api", data={"csrf_token": token, "enabled": "1", "priority": "3",
                                                         "rate_limit_per_minute": "4", "automated_access": "unverified"})
    assert r.status_code == 302


def test_api_envelopes_and_internal_token(client, admin):
    r = client.get("/api/v1/trains/0000/status")
    body = r.get_json()
    assert set(body) >= {"source", "fetched_at", "status", "data", "cached", "error"}
    assert client.get("/api/v1/jobs").status_code == 401
    r = client.post("/api/v1/jobs/pnr_checks/pause", headers={"X-Internal-Token": "test-internal-token"})
    assert r.status_code == 200 and r.get_json()["enabled"] is False
    assert client.get("/api/v1/jobs", headers={"X-Internal-Token": "wrong"}).status_code == 401


def test_schema_file_matches_models(app):
    from railmon.cli import render_mysql_schema

    sql = render_mysql_schema()
    for table in ["trains", "stations", "train_routes", "train_schedules", "train_live_status", "train_disruptions", "pnrs",
                  "pnr_passengers", "pnr_status_history", "chart_status_history", "api_sources", "source_health",
                  "monitoring_jobs", "system_logs"]:
        assert f"CREATE TABLE {table} (" in sql, table
    committed = (Path(__file__).resolve().parent.parent / "sql" / "schema.sql").read_text()
    assert committed == sql, "sql/schema.sql is stale: run `python -m railmon.dump_schema > sql/schema.sql`"
