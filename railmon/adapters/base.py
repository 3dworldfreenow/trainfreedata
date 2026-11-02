"""Source adapter interface.

Every external data source has its own adapter subclass. Adapter methods return
an :class:`AdapterResponse`; :class:`railmon.services.sources.SourceManager`
wraps it into the normalised envelope::

    {"source", "fetched_at", "status", "data", "cached", "error",
     "data_kind", "manual_enquiry"}

Adapters must never fabricate data. If a source cannot be queried
automatically (no documented API, CAPTCHA, ToS), return ``manual_required``
with the official URL so the user can check by hand.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, ClassVar

# Result statuses
OK = "ok"
NOT_FOUND = "not_found"
UNSUPPORTED = "unsupported"
MANUAL_REQUIRED = "manual_required"
RATE_LIMITED = "rate_limited"
ERROR = "error"
UNAVAILABLE = "unavailable"
DISABLED = "disabled"

CAPABILITIES = (
    "train_status",
    "station_board",
    "pnr_status",
    "chart_status",
    "cancelled_trains",
    "rescheduled_trains",
    "diverted_trains",
    "train_schedule",
    "station_schedule",
)


@dataclass
class AdapterResponse:
    status: str
    data: Any = None
    error: str | None = None
    data_kind: str = "live"  # live | static | manual
    manual_url: str | None = None
    manual_instructions: str | None = None
    http_status: int | None = None
    latency_ms: int | None = None
    network: bool = True  # whether a network request was made (for health tracking)


@dataclass
class HealthResult:
    status: str  # up | degraded | down | manual_only | not_applicable
    http_status: int | None = None
    latency_ms: int | None = None
    error: str | None = None
    details: dict = field(default_factory=dict)


class NotSupported(Exception):
    pass


class BaseAdapter:
    #: adapter identifier referenced by ``api_sources.adapter``
    adapter_name: ClassVar[str] = "base"
    capabilities: ClassVar[set[str]] = set()
    #: True for adapters that can only point users to a manual enquiry page
    manual_only: ClassVar[bool] = False
    #: True for adapters that read local data only (no network)
    local_only: ClassVar[bool] = False

    def __init__(self, source: dict, http, config) -> None:
        self.source = source  # plain dict snapshot of the api_sources row
        self.http = http
        self.config = config
        self.settings: dict = source.get("config") or {}

    @property
    def key(self) -> str:
        return self.source["key"]

    def supports(self, capability: str) -> bool:
        return capability in self.capabilities

    # -- interface (override what the source supports) ---------------------------
    def get_train_status(self, train_number: str, journey_date: date | None = None) -> AdapterResponse:
        raise NotSupported

    def get_station_board(self, station_code: str, window_hours: int = 2) -> AdapterResponse:
        raise NotSupported

    def get_pnr_status(self, pnr_number: str) -> AdapterResponse:
        raise NotSupported

    def get_chart_status(self, pnr_number: str) -> AdapterResponse:
        raise NotSupported

    def get_cancelled_trains(self, on_date: date) -> AdapterResponse:
        raise NotSupported

    def get_rescheduled_trains(self, on_date: date) -> AdapterResponse:
        raise NotSupported

    def get_diverted_trains(self, on_date: date) -> AdapterResponse:
        raise NotSupported

    def get_train_schedule(self, train_number: str) -> AdapterResponse:
        raise NotSupported

    def get_station_schedule(self, station_code: str, window_hours: int = 4) -> AdapterResponse:
        raise NotSupported

    def health_check(self) -> HealthResult:
        return HealthResult(status="not_applicable")


METHOD_FOR = {
    "train_status": "get_train_status",
    "station_board": "get_station_board",
    "pnr_status": "get_pnr_status",
    "chart_status": "get_chart_status",
    "cancelled_trains": "get_cancelled_trains",
    "rescheduled_trains": "get_rescheduled_trains",
    "diverted_trains": "get_diverted_trains",
    "train_schedule": "get_train_schedule",
    "station_schedule": "get_station_schedule",
}
