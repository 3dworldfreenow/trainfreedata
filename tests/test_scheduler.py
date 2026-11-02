
from railmon.extensions import cache, db
from railmon.models import MonitoringJob, Train, TrainDisruption, TrainLiveStatus
from railmon.scheduler import jobs as jobs_mod
from railmon.scheduler.runner import run_job, sync_jobs

from .conftest import add_fake_source


def _job(key):
    db.session.expire_all()
    return db.session.query(MonitoringJob).filter_by(job_key=key).one()


def test_jobs_seeded(app):
    keys = {j.job_key for j in db.session.query(MonitoringJob)}
    assert keys == set(jobs_mod.JOBS)
    assert sync_jobs(app.config["SCHEDULER_CONFIG_PATH"]) == 0  # idempotent
    assert _job("train_status").interval_seconds == 300


def test_skips_without_automated_source(app):
    assert run_job(app, "train_status") == "skipped"
    assert "no enabled source" in _job("train_status").last_message


def test_paused_job_not_run_unless_manual(app):
    j = _job("source_health")
    j.enabled = False
    db.session.commit()
    assert run_job(app, "source_health") == "paused"
    assert _job("source_health").run_count == 0


def test_overlap_prevented(app):
    with cache.lock("job:daily_summary", ttl=60):
        assert run_job(app, "daily_summary") == "overlap_skipped"
    assert run_job(app, "daily_summary") == "success"


def test_failure_is_recorded_not_raised(app, monkeypatch):
    def boom(job):
        raise RuntimeError("kaput")

    monkeypatch.setitem(jobs_mod.JOBS, "retention", boom)
    assert run_job(app, "retention") == "failed"
    j = _job("retention")
    assert j.failure_count == 1 and "kaput" in j.last_message


def test_train_status_job_stores_snapshots_on_change(app):
    db.session.add(Train(number="12002", name="Test", is_monitored=True))
    db.session.commit()
    add_fake_source(data={"train_status": {"running_status": "running", "delay_minutes": 20, "last_reported_station_code": "AGC"}})
    assert run_job(app, "train_status") == "success"
    cache.client.flushall()  # force a new fetch with identical data
    assert run_job(app, "train_status") == "success"
    rows = db.session.query(TrainLiveStatus).all()
    assert len(rows) == 1 and rows[0].delay_minutes == 20 and rows[0].data_kind == "live"


def test_disruption_job(app):
    add_fake_source(capabilities=("cancelled_trains",), data={"cancelled_trains": {"trains": [
        {"train_number": "12002", "train_name": "Shatabdi", "reason": "Maintenance"}]}})
    assert run_job(app, "disruptions") == "success"
    rows = db.session.query(TrainDisruption).all()
    assert {r.disruption_type for r in rows} == {"cancelled"} and rows[0].reason == "Maintenance"
    assert run_job(app, "disruptions") == "success"
    assert db.session.query(TrainDisruption).count() == len(rows)  # upserted, not duplicated


def test_daily_summary_and_retention(app):
    assert run_job(app, "daily_summary") == "success"
    assert run_job(app, "retention") == "success"
