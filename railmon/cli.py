"""``flask railmon <command>`` management commands."""
from __future__ import annotations

import getpass
from pathlib import Path

import click
import yaml
from flask import current_app
from flask.cli import AppGroup
from werkzeug.security import generate_password_hash

from .extensions import db
from .models import ApiSource, Station, Train, User

railmon_cli = AppGroup("railmon", help="RailMon management commands")


def sync_sources(path: str, reset: bool = False) -> int:
    data = yaml.safe_load(Path(path).read_text()) or {}
    added = 0
    for spec in data.get("sources", []):
        row = db.session.query(ApiSource).filter_by(key=spec["key"]).first()
        new = row is None
        if new:
            row = ApiSource(key=spec["key"])
            db.session.add(row)
            added += 1
        for k in ("name", "adapter", "base_url", "documentation_url", "notes"):
            setattr(row, k, spec.get(k))
        row.automated_access = spec.get("automated_access", "unverified") if (new or reset or row.automated_access is None) else row.automated_access
        if new or reset:
            row.enabled = spec.get("enabled", True)
            row.priority = spec.get("priority", 100)
            row.rate_limit_per_minute = spec.get("rate_limit_per_minute", 6)
            row.config = spec.get("config") or {}
    db.session.commit()
    return added


@railmon_cli.command("init-db")
def init_db():
    """Create tables (if missing) and seed sources + monitoring jobs."""
    from .scheduler.runner import sync_jobs

    db.create_all()
    s = sync_sources(current_app.config["SOURCES_CONFIG_PATH"])
    j = sync_jobs(current_app.config["SCHEDULER_CONFIG_PATH"])
    click.echo(f"database ready; {s} sources and {j} jobs added")


@railmon_cli.command("sync-sources")
@click.option("--reset", is_flag=True, help="Also reset enabled/priority/rate limit/config/automated_access")
def sync_sources_cmd(reset):
    click.echo(f"{sync_sources(current_app.config['SOURCES_CONFIG_PATH'], reset)} sources added")


@railmon_cli.command("sync-jobs")
@click.option("--reset", is_flag=True, help="Re-apply schedule/enabled/config from the YAML file")
def sync_jobs_cmd(reset):
    from .scheduler.runner import sync_jobs

    click.echo(f"{sync_jobs(current_app.config['SCHEDULER_CONFIG_PATH'], reset)} jobs added")


@railmon_cli.command("create-user")
@click.argument("username")
@click.option("--admin", is_flag=True)
@click.option("--password", default=None, help="Omit to be prompted")
def create_user(username, admin, password):
    password = password or getpass.getpass("Password: ")
    if len(password) < 10:
        raise click.ClickException("password must be at least 10 characters")
    user = db.session.query(User).filter_by(username=username).first() or User(username=username)
    user.password_hash = generate_password_hash(password)
    user.role = "admin" if admin else "user"
    db.session.add(user)
    db.session.commit()
    click.echo(f"user {username} saved ({user.role})")


@railmon_cli.command("import-datameet")
@click.option("--path", default=None, help="Directory containing stations.json, trains.json, schedules.json")
@click.option("--download/--no-download", default=True, help="Download the files if missing")
def import_datameet(path, download):
    """Import the CC0 datameet static timetable (stations, trains, routes)."""
    from .importers import datameet

    target = Path(path or Path(current_app.root_path).parent / "data" / "datameet")
    if download:
        datameet.download(target, current_app.config["HTTP_USER_AGENT"])
    datameet.import_dir(target, echo=click.echo)


@railmon_cli.command("monitor-train")
@click.argument("numbers", nargs=-1, required=True)
@click.option("--off", is_flag=True)
def monitor_train(numbers, off):
    n = db.session.query(Train).filter(Train.number.in_(numbers)).update({"is_monitored": not off}, synchronize_session=False)
    db.session.commit()
    click.echo(f"{n} trains updated")


@railmon_cli.command("monitor-station")
@click.argument("codes", nargs=-1, required=True)
@click.option("--off", is_flag=True)
def monitor_station(codes, off):
    codes = [c.upper() for c in codes]
    n = db.session.query(Station).filter(Station.code.in_(codes)).update({"is_monitored": not off}, synchronize_session=False)
    db.session.commit()
    click.echo(f"{n} stations updated")


@railmon_cli.command("probe-sources")
def probe_sources():
    """Run a health probe against every source now and print the result."""
    from .services.health import probe_all

    for key, status in probe_all().items():
        click.echo(f"{key:20s} {status}")


@railmon_cli.command("run-job")
@click.argument("job_key")
def run_job_cmd(job_key):
    from .scheduler.runner import run_job

    click.echo(run_job(current_app._get_current_object(), job_key, manual=True))


@railmon_cli.command("scheduler")
def scheduler_cmd():
    """Run the scheduler worker in the foreground."""
    from .scheduler.runner import Worker

    Worker(current_app._get_current_object()).run_forever()


@railmon_cli.command("dump-schema")
@click.option("--output", default=None)
def dump_schema(output):
    """Write MySQL DDL generated from the models (sql/schema.sql)."""
    click.echo(render_mysql_schema(), file=open(output, "w") if output else None)


def render_mysql_schema() -> str:
    from sqlalchemy.dialects import mysql
    from sqlalchemy.schema import CreateIndex, CreateTable

    dialect = mysql.dialect()
    parts = [
        "-- RailMon MySQL schema (generated by `flask railmon dump-schema`; do not edit by hand)",
        "-- MySQL 8.0+, utf8mb4. All timestamps are UTC.",
        "SET NAMES utf8mb4;",
        "SET FOREIGN_KEY_CHECKS = 0;",
        "",
    ]
    for table in db.metadata.sorted_tables:
        ddl = str(CreateTable(table).compile(dialect=dialect)).strip()
        parts.append(f"DROP TABLE IF EXISTS `{table.name}`;")
        parts.append(ddl + " ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;")
        for idx in sorted(table.indexes, key=lambda i: i.name):
            parts.append(str(CreateIndex(idx).compile(dialect=dialect)).strip() + ";")
        parts.append("")
    parts.append("SET FOREIGN_KEY_CHECKS = 1;")
    return "\n".join(parts) + "\n"


def register_cli(app) -> None:
    app.cli.add_command(railmon_cli)
