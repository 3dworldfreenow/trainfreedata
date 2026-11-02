"""APScheduler-based worker.

* Jobs are defined in the ``monitoring_jobs`` table (seeded from config/scheduler.yaml).
* ``max_instances=1`` + ``coalesce`` + a Redis lock per job => no overlapping runs,
  even with several worker processes.
* A control loop (every 15 s) applies admin changes: pause/resume, interval/cron
  edits, and "run now" requests from the dashboard.
"""
from __future__ import annotations

import logging
import signal
import time
from datetime import timezone
from pathlib import Path

import yaml
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from flask import Flask

from ..extensions import cache, db
from ..models import MonitoringJob
from ..syslog import log_event
from ..timeutil import utcnow
from .jobs import JOBS, Skipped

log = logging.getLogger(__name__)


def sync_jobs(path: str, reset: bool = False) -> int:
    data = yaml.safe_load(Path(path).read_text()) or {}
    n = 0
    for spec in data.get("jobs", []):
        if spec["key"] not in JOBS:
            raise ValueError(f"unknown job key {spec['key']}")
        row = db.session.query(MonitoringJob).filter_by(job_key=spec["key"]).first()
        new = row is None
        if new:
            row = MonitoringJob(job_key=spec["key"])
            db.session.add(row)
            n += 1
        row.name = spec["name"]
        row.description = spec.get("description")
        if new or reset:
            row.trigger_type = spec["trigger"]
            row.interval_seconds = spec.get("interval_seconds")
            row.cron = spec.get("cron")
            row.config = spec.get("config") or {}
            row.enabled = spec.get("enabled", True)
    db.session.commit()
    return n


def run_job(app: Flask, job_key: str, manual: bool = False) -> str:
    """Execute one job with pause check, overlap lock and bookkeeping."""
    with app.app_context():
        job = db.session.query(MonitoringJob).filter_by(job_key=job_key).first()
        if job is None:
            return "missing"
        if not job.enabled and not manual:
            return "paused"
        with cache.lock(f"job:{job_key}", ttl=max(600, (job.interval_seconds or 0) * 2)) as acquired:
            if not acquired:
                log_event("INFO", "scheduler", "job already running elsewhere; skipped", job=job_key)
                return "overlap_skipped"
            started = utcnow()
            job.last_started_at, job.last_status = started, "running"
            job.run_requested_at = None
            db.session.commit()
            t0 = time.monotonic()
            try:
                msg = JOBS[job_key](job)
                status = "success"
            except Skipped as exc:
                msg, status = str(exc), "skipped"
            except Exception as exc:  # noqa: BLE001 - one job must never kill the scheduler
                db.session.rollback()
                msg, status = f"{type(exc).__name__}: {exc}", "failed"
                log_event("ERROR", "scheduler", "job failed", job=job_key, error=msg)
            job = db.session.query(MonitoringJob).filter_by(job_key=job_key).first()
            job.last_finished_at = utcnow()
            job.last_duration_ms = int((time.monotonic() - t0) * 1000)
            job.last_status, job.last_message = status, (msg or "")[:2000]
            job.run_count += 1
            if status == "success":
                job.last_success_at = job.last_finished_at
            elif status == "failed":
                job.failure_count += 1
            db.session.commit()
            return status


class Worker:
    def __init__(self, app: Flask) -> None:
        self.app = app
        self.sched = BackgroundScheduler(timezone=app.config["TIMEZONE"], job_defaults={
            "coalesce": True, "max_instances": 1, "misfire_grace_time": 120,
        })
        self._signatures: dict[str, tuple] = {}
        self._stop = False

    def _trigger(self, job: MonitoringJob):
        if job.trigger_type == "cron" and job.cron:
            return CronTrigger.from_crontab(job.cron, timezone=self.app.config["TIMEZONE"])
        return IntervalTrigger(seconds=max(60, job.interval_seconds or 300))

    def reconcile(self) -> None:
        with self.app.app_context():
            jobs = db.session.query(MonitoringJob).all()
            for job in jobs:
                if job.job_key not in JOBS:
                    continue
                sig = (job.enabled, job.trigger_type, job.interval_seconds, job.cron)
                if self._signatures.get(job.job_key) != sig:
                    if self.sched.get_job(job.job_key):
                        self.sched.remove_job(job.job_key)
                    if job.enabled:
                        self.sched.add_job(run_job, self._trigger(job), args=[self.app, job.job_key],
                                           id=job.job_key, name=job.name, replace_existing=True)
                    self._signatures[job.job_key] = sig
                    log_event("INFO", "scheduler", "job (re)scheduled", job=job.job_key, enabled=job.enabled)
                if job.run_requested_at:
                    self.sched.add_job(run_job, args=[self.app, job.job_key, True], id=f"{job.job_key}:now",
                                       replace_existing=True, max_instances=1)
                    job.run_requested_at = None
                aps = self.sched.get_job(job.job_key)
                job.next_run_at = (
                    aps.next_run_time.astimezone(timezone.utc).replace(tzinfo=None) if aps and aps.next_run_time else None
                )
            db.session.commit()

    def run_forever(self) -> None:
        signal.signal(signal.SIGTERM, lambda *_: setattr(self, "_stop", True))
        signal.signal(signal.SIGINT, lambda *_: setattr(self, "_stop", True))
        with self.app.app_context():
            sync_jobs(self.app.config["SCHEDULER_CONFIG_PATH"])
            log_event("INFO", "scheduler", "worker started")
        self.sched.start()
        try:
            while not self._stop:
                try:
                    self.reconcile()
                except Exception as exc:  # noqa: BLE001
                    log.exception("reconcile failed: %s", exc)
                for _ in range(15):  # stay responsive to SIGTERM
                    if self._stop:
                        break
                    time.sleep(1)
        finally:
            self.sched.shutdown(wait=True)
            with self.app.app_context():
                log_event("INFO", "scheduler", "worker stopped")


def main() -> None:
    from .. import create_app

    Worker(create_app()).run_forever()


if __name__ == "__main__":
    main()
