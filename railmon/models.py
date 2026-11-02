"""SQLAlchemy models. ``sql/schema.sql`` is generated from these (``flask railmon dump-schema``).

All timestamps are stored as naive UTC datetimes.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .extensions import db

# BIGINT on MySQL, INTEGER on SQLite (so autoincrement works in tests)
BigId = BigInteger().with_variant(Integer, "sqlite")


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


DATA_KINDS = ("live", "cached", "static", "manual")
DataKind = Enum(*DATA_KINDS, name="data_kind")


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)


# --------------------------------------------------------------------------- users
class User(TimestampMixin, db.Model):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(Enum("admin", "user", name="user_role"), default="user", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime)


# --------------------------------------------------------------------------- sources
class ApiSource(TimestampMixin, db.Model):
    __tablename__ = "api_sources"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    adapter: Mapped[str] = mapped_column(String(64), nullable=False)
    base_url: Mapped[str | None] = mapped_column(String(255))
    documentation_url: Mapped[str | None] = mapped_column(String(255))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    priority: Mapped[int] = mapped_column(SmallInteger, default=100, nullable=False)
    # Whether automated (programmatic) access is permitted by the source's terms.
    automated_access: Mapped[str] = mapped_column(
        Enum("permitted", "not_permitted", "unverified", "not_applicable", name="automated_access"),
        default="unverified",
        nullable=False,
    )
    rate_limit_per_minute: Mapped[int] = mapped_column(Integer, default=6, nullable=False)
    capabilities: Mapped[list | None] = mapped_column(JSON)
    config: Mapped[dict | None] = mapped_column(JSON)
    notes: Mapped[str | None] = mapped_column(Text)
    last_status: Mapped[str] = mapped_column(String(32), default="unknown", nullable=False)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_failure_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_error: Mapped[str | None] = mapped_column(Text)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class SourceHealth(db.Model):
    __tablename__ = "source_health"
    __table_args__ = (Index("ix_source_health_source_checked", "source_id", "checked_at"),)
    id: Mapped[int] = mapped_column(BigId, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("api_sources.id", ondelete="CASCADE"), nullable=False)
    checked_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    check_type: Mapped[str] = mapped_column(Enum("probe", "request", name="health_check_type"), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)  # up/down/degraded/manual_only/disabled
    capability: Mapped[str | None] = mapped_column(String(64))
    http_status: Mapped[int | None] = mapped_column(SmallInteger)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    source = relationship("ApiSource")


# --------------------------------------------------------------------------- master data
class Station(TimestampMixin, db.Model):
    __tablename__ = "stations"
    __table_args__ = (Index("ix_stations_name", "name"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(10), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    state: Mapped[str | None] = mapped_column(String(64))
    zone: Mapped[str | None] = mapped_column(String(16))
    address: Mapped[str | None] = mapped_column(String(255))
    latitude: Mapped[float | None] = mapped_column(db.Float)
    longitude: Mapped[float | None] = mapped_column(db.Float)
    is_monitored: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    data_source: Mapped[str] = mapped_column(String(64), default="datameet", nullable=False)


class Train(TimestampMixin, db.Model):
    __tablename__ = "trains"
    __table_args__ = (Index("ix_trains_name", "name"), Index("ix_trains_monitored", "is_monitored"))
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    number: Mapped[str] = mapped_column(String(10), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    train_type: Mapped[str | None] = mapped_column(String(32))
    zone: Mapped[str | None] = mapped_column(String(16))
    source_station_code: Mapped[str | None] = mapped_column(
        String(10), ForeignKey("stations.code", onupdate="CASCADE", ondelete="SET NULL")
    )
    destination_station_code: Mapped[str | None] = mapped_column(
        String(10), ForeignKey("stations.code", onupdate="CASCADE", ondelete="SET NULL")
    )
    departure_time: Mapped[str | None] = mapped_column(String(8))  # HH:MM:SS at source
    arrival_time: Mapped[str | None] = mapped_column(String(8))  # HH:MM:SS at destination
    duration_minutes: Mapped[int | None] = mapped_column(Integer)
    distance_km: Mapped[int | None] = mapped_column(Integer)
    # 7 chars Mon..Sun, e.g. "1111100"; NULL = unknown (the datameet dataset has no running days)
    running_days: Mapped[str | None] = mapped_column(String(7))
    classes: Mapped[list | None] = mapped_column(JSON)
    return_train: Mapped[str | None] = mapped_column(String(10))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_monitored: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    data_source: Mapped[str] = mapped_column(String(64), default="datameet", nullable=False)

    source_station = relationship("Station", foreign_keys=[source_station_code])
    destination_station = relationship("Station", foreign_keys=[destination_station_code])


class TrainSchedule(db.Model):
    """A timetable version for a train. Routes hang off a schedule so timetable changes keep history."""

    __tablename__ = "train_schedules"
    __table_args__ = (Index("ix_train_schedules_train_current", "train_id", "is_current"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    train_id: Mapped[int] = mapped_column(ForeignKey("trains.id", ondelete="CASCADE"), nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    valid_from: Mapped[datetime | None] = mapped_column(Date)
    valid_to: Mapped[datetime | None] = mapped_column(Date)
    running_days: Mapped[str | None] = mapped_column(String(7))
    data_source: Mapped[str] = mapped_column(String(64), nullable=False)
    schedule_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    stop_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    imported_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    train = relationship("Train", backref="schedules")


class TrainRoute(db.Model):
    __tablename__ = "train_routes"
    __table_args__ = (
        UniqueConstraint("schedule_id", "stop_sequence", name="uq_route_schedule_seq"),
        Index("ix_train_routes_station", "station_code"),
        Index("ix_train_routes_train", "train_id"),
    )
    id: Mapped[int] = mapped_column(BigId, primary_key=True)
    schedule_id: Mapped[int] = mapped_column(ForeignKey("train_schedules.id", ondelete="CASCADE"), nullable=False)
    train_id: Mapped[int] = mapped_column(ForeignKey("trains.id", ondelete="CASCADE"), nullable=False)
    stop_sequence: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    station_code: Mapped[str] = mapped_column(
        String(10), ForeignKey("stations.code", onupdate="CASCADE"), nullable=False
    )
    arrival_time: Mapped[str | None] = mapped_column(String(8))
    departure_time: Mapped[str | None] = mapped_column(String(8))
    day_offset: Mapped[int | None] = mapped_column(SmallInteger)  # 1 = day of departure from source
    distance_km: Mapped[int | None] = mapped_column(Integer)
    is_halt: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    station = relationship("Station")


# --------------------------------------------------------------------------- live data
class TrainLiveStatus(db.Model):
    """Running-status snapshot. A row is written only when the content changes."""

    __tablename__ = "train_live_status"
    __table_args__ = (
        Index("ix_live_train_journey_fetched", "train_id", "journey_date", "fetched_at"),
        Index("ix_live_fetched", "fetched_at"),
        Index("ix_live_delay", "delay_minutes"),
    )
    id: Mapped[int] = mapped_column(BigId, primary_key=True)
    train_id: Mapped[int | None] = mapped_column(ForeignKey("trains.id", ondelete="CASCADE"))
    train_number: Mapped[str] = mapped_column(String(10), nullable=False)
    journey_date: Mapped[datetime | None] = mapped_column(Date)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("api_sources.id", ondelete="SET NULL"))
    data_kind: Mapped[str] = mapped_column(DataKind, nullable=False)
    running_status: Mapped[str | None] = mapped_column(String(32))  # running/arrived/not_started/cancelled/...
    current_station_code: Mapped[str | None] = mapped_column(String(10))
    last_reported_station_code: Mapped[str | None] = mapped_column(String(10))
    last_reported_at: Mapped[datetime | None] = mapped_column(DateTime)
    next_station_code: Mapped[str | None] = mapped_column(String(10))
    delay_minutes: Mapped[int | None] = mapped_column(Integer)
    eta: Mapped[datetime | None] = mapped_column(DateTime)
    etd: Mapped[datetime | None] = mapped_column(DateTime)
    payload: Mapped[dict | None] = mapped_column(JSON)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    last_confirmed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    source = relationship("ApiSource")
    train = relationship("Train")


class StationBoardSnapshot(db.Model):
    __tablename__ = "station_board_snapshots"
    __table_args__ = (Index("ix_board_station_fetched", "station_code", "fetched_at"),)
    id: Mapped[int] = mapped_column(BigId, primary_key=True)
    station_code: Mapped[str] = mapped_column(
        String(10), ForeignKey("stations.code", onupdate="CASCADE", ondelete="CASCADE"), nullable=False
    )
    source_id: Mapped[int | None] = mapped_column(ForeignKey("api_sources.id", ondelete="SET NULL"))
    data_kind: Mapped[str] = mapped_column(DataKind, nullable=False)
    window_hours: Mapped[int] = mapped_column(SmallInteger, default=2, nullable=False)
    entries: Mapped[list] = mapped_column(JSON, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    last_confirmed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    source = relationship("ApiSource")


DISRUPTION_TYPES = (
    "cancelled",
    "partially_cancelled",
    "rescheduled",
    "diverted",
    "short_terminated",
    "short_originated",
)


class TrainDisruption(db.Model):
    __tablename__ = "train_disruptions"
    __table_args__ = (
        UniqueConstraint("train_number", "journey_date", "disruption_type", "source_id", name="uq_disruption"),
        Index("ix_disruptions_date_type", "journey_date", "disruption_type"),
        Index("ix_disruptions_train", "train_number"),
    )
    id: Mapped[int] = mapped_column(BigId, primary_key=True)
    train_id: Mapped[int | None] = mapped_column(ForeignKey("trains.id", ondelete="SET NULL"))
    train_number: Mapped[str] = mapped_column(String(10), nullable=False)
    train_name: Mapped[str | None] = mapped_column(String(160))
    disruption_type: Mapped[str] = mapped_column(Enum(*DISRUPTION_TYPES, name="disruption_type"), nullable=False)
    journey_date: Mapped[datetime] = mapped_column(Date, nullable=False)
    from_station_code: Mapped[str | None] = mapped_column(String(10))
    to_station_code: Mapped[str | None] = mapped_column(String(10))
    affected_from_code: Mapped[str | None] = mapped_column(String(10))
    affected_to_code: Mapped[str | None] = mapped_column(String(10))
    rescheduled_departure: Mapped[datetime | None] = mapped_column(DateTime)
    rescheduled_by_minutes: Mapped[int | None] = mapped_column(Integer)
    diverted_route: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(String(255))
    source_id: Mapped[int | None] = mapped_column(ForeignKey("api_sources.id", ondelete="SET NULL"))
    data_kind: Mapped[str] = mapped_column(DataKind, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    source = relationship("ApiSource")


# --------------------------------------------------------------------------- PNR
CHART_STATUSES = ("unknown", "not_prepared", "prepared")


class Pnr(TimestampMixin, db.Model):
    __tablename__ = "pnrs"
    __table_args__ = (
        UniqueConstraint("user_id", "pnr_hash", name="uq_pnr_user_hash"),
        Index("ix_pnrs_next_check", "monitoring_enabled", "next_check_at"),
        Index("ix_pnrs_journey", "journey_date"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    pnr_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    pnr_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    pnr_last4: Mapped[str] = mapped_column(String(4), nullable=False)
    label: Mapped[str | None] = mapped_column(String(64))
    train_number: Mapped[str | None] = mapped_column(String(10))
    train_name: Mapped[str | None] = mapped_column(String(160))
    journey_date: Mapped[datetime | None] = mapped_column(Date)
    boarding_station_code: Mapped[str | None] = mapped_column(String(10))
    from_station_code: Mapped[str | None] = mapped_column(String(10))
    to_station_code: Mapped[str | None] = mapped_column(String(10))
    travel_class: Mapped[str | None] = mapped_column(String(8))
    # Scheduled departure from boarding station, derived from the static timetable.
    # Used ONLY to decide how often to check - never to infer chart preparation.
    scheduled_departure_at: Mapped[datetime | None] = mapped_column(DateTime)
    monitoring_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    check_interval_minutes: Mapped[int | None] = mapped_column(Integer)  # NULL = adaptive
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_check_result: Mapped[str | None] = mapped_column(String(32))
    next_check_at: Mapped[datetime | None] = mapped_column(DateTime)
    next_chart_check_at: Mapped[datetime | None] = mapped_column(DateTime)
    check_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    chart_status: Mapped[str] = mapped_column(Enum(*CHART_STATUSES, name="chart_status"), default="unknown", nullable=False)
    chart_status_changed_at: Mapped[datetime | None] = mapped_column(DateTime)
    chart_status_source: Mapped[str | None] = mapped_column(String(64))

    passengers = relationship(
        "PnrPassenger", cascade="all, delete-orphan", passive_deletes=True, order_by="PnrPassenger.serial"
    )
    history = relationship(
        "PnrStatusHistory", cascade="all, delete-orphan", passive_deletes=True, order_by="PnrStatusHistory.checked_at.desc()"
    )
    chart_history = relationship(
        "ChartStatusHistory", cascade="all, delete-orphan", passive_deletes=True, order_by="ChartStatusHistory.observed_at.desc()"
    )
    user = relationship("User")


class PnrPassenger(db.Model):
    """Per-passenger status. No names or ages are stored."""

    __tablename__ = "pnr_passengers"
    __table_args__ = (UniqueConstraint("pnr_id", "serial", name="uq_pnr_passenger_serial"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pnr_id: Mapped[int] = mapped_column(ForeignKey("pnrs.id", ondelete="CASCADE"), nullable=False)
    serial: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    booking_status: Mapped[str | None] = mapped_column(String(32))  # raw, e.g. "WL/23", "RAC 5", "CNF/B2/45"
    booking_code: Mapped[str | None] = mapped_column(String(8))  # CNF/RAC/WL/RLWL/PQWL/GNWL/...
    current_status: Mapped[str | None] = mapped_column(String(32))
    current_code: Mapped[str | None] = mapped_column(String(8))
    coach: Mapped[str | None] = mapped_column(String(8))
    berth: Mapped[str | None] = mapped_column(String(8))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)


class PnrStatusHistory(db.Model):
    __tablename__ = "pnr_status_history"
    __table_args__ = (Index("ix_pnr_history_pnr_checked", "pnr_id", "checked_at"),)
    id: Mapped[int] = mapped_column(BigId, primary_key=True)
    pnr_id: Mapped[int] = mapped_column(ForeignKey("pnrs.id", ondelete="CASCADE"), nullable=False)
    checked_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("api_sources.id", ondelete="SET NULL"))
    data_kind: Mapped[str] = mapped_column(DataKind, nullable=False)
    chart_status: Mapped[str] = mapped_column(Enum(*CHART_STATUSES, name="chart_status"), nullable=False)
    snapshot_encrypted: Mapped[str] = mapped_column(Text, nullable=False)  # encrypted passenger statuses
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    changed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    change_summary: Mapped[str | None] = mapped_column(Text)
    source = relationship("ApiSource")


class ChartStatusHistory(db.Model):
    __tablename__ = "chart_status_history"
    __table_args__ = (Index("ix_chart_history_pnr_observed", "pnr_id", "observed_at"),)
    id: Mapped[int] = mapped_column(BigId, primary_key=True)
    pnr_id: Mapped[int] = mapped_column(ForeignKey("pnrs.id", ondelete="CASCADE"), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    previous_status: Mapped[str] = mapped_column(Enum(*CHART_STATUSES, name="chart_status"), nullable=False)
    chart_status: Mapped[str] = mapped_column(Enum(*CHART_STATUSES, name="chart_status"), nullable=False)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("api_sources.id", ondelete="SET NULL"))
    data_kind: Mapped[str] = mapped_column(DataKind, nullable=False)
    source = relationship("ApiSource")


# --------------------------------------------------------------------------- operations
class MonitoringJob(TimestampMixin, db.Model):
    __tablename__ = "monitoring_jobs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)  # False = paused
    trigger_type: Mapped[str] = mapped_column(Enum("interval", "cron", name="job_trigger"), nullable=False)
    interval_seconds: Mapped[int | None] = mapped_column(Integer)
    cron: Mapped[str | None] = mapped_column(String(64))  # 5-field cron in TIMEZONE
    config: Mapped[dict | None] = mapped_column(JSON)
    run_requested_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_started_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_status: Mapped[str] = mapped_column(String(32), default="never_run", nullable=False)
    last_message: Mapped[str | None] = mapped_column(Text)
    last_duration_ms: Mapped[int | None] = mapped_column(Integer)
    run_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failure_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime)


class SystemLog(db.Model):
    __tablename__ = "system_logs"
    __table_args__ = (Index("ix_system_logs_created", "created_at"), Index("ix_system_logs_level", "level", "created_at"))
    id: Mapped[int] = mapped_column(BigId, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    level: Mapped[str] = mapped_column(Enum("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL", name="log_level"), nullable=False)
    component: Mapped[str] = mapped_column(String(64), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    context: Mapped[dict | None] = mapped_column(JSON)


class DailyReport(db.Model):
    __tablename__ = "daily_reports"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    report_date: Mapped[datetime] = mapped_column(Date, unique=True, nullable=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    summary: Mapped[dict] = mapped_column(JSON, nullable=False)
