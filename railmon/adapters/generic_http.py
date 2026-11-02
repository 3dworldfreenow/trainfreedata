"""Configurable JSON-over-HTTP adapter for sources with *documented, permitted* access.

Use this to plug in any free source you are allowed to query programmatically
(e.g. a future official open API, or your own bridge service). It refuses to
run unless the source's ``automated_access`` is set to ``permitted``.

``api_sources.config`` example::

    {
      "endpoints": {
        "train_status": "https://example.org/api/trains/{train_number}/status?date={journey_date}",
        "station_board": "https://example.org/api/stations/{station_code}/board?hours={window_hours}",
        "cancelled_trains": "https://example.org/api/disruptions/cancelled?date={date}"
      },
      "headers": {"Accept": "application/json"},
      "api_key_env": "EXAMPLE_API_KEY",          # optional: value read from environment
      "api_key_header": "X-API-Key"
    }

Each endpoint must return either the normalised ``data`` object directly, or an
envelope ``{"status": "ok"|"not_found", "data": {...}}`` using the shapes in
docs/DATA_SOURCES.md.
"""
from __future__ import annotations

import os
from datetime import date

from .base import DISABLED, ERROR, NOT_FOUND, OK, AdapterResponse, BaseAdapter, HealthResult


class GenericHttpAdapter(BaseAdapter):
    adapter_name = "generic_http"

    @property
    def capabilities(self):  # type: ignore[override]
        return set((self.settings.get("endpoints") or {}).keys())

    def _call(self, capability: str, **params) -> AdapterResponse:
        from ..http import RobotsDisallowed

        if self.source.get("automated_access") != "permitted":
            return AdapterResponse(
                status=DISABLED,
                network=False,
                error="generic_http sources require automated_access='permitted' (documented permission)",
            )
        template = (self.settings.get("endpoints") or {}).get(capability)
        if not template:
            return AdapterResponse(status=DISABLED, network=False, error=f"no endpoint configured for {capability}")
        url = template.format(**{k: ("" if v is None else v) for k, v in params.items()})
        headers = dict(self.settings.get("headers") or {})
        key_env = self.settings.get("api_key_env")
        if key_env and os.getenv(key_env):
            headers[self.settings.get("api_key_header", "Authorization")] = os.environ[key_env]
        try:
            res = self.http.get(url, headers=headers, check_robots=self.settings.get("check_robots", True))
        except RobotsDisallowed as exc:
            return AdapterResponse(status=ERROR, error=str(exc))
        if res.response is None:
            return AdapterResponse(status=ERROR, error=res.error, latency_ms=res.latency_ms)
        code = res.response.status_code
        if code == 404:
            return AdapterResponse(status=NOT_FOUND, http_status=code, latency_ms=res.latency_ms)
        if code >= 400:
            return AdapterResponse(status=ERROR, error=res.error, http_status=code, latency_ms=res.latency_ms)
        try:
            body = res.response.json()
        except ValueError:
            return AdapterResponse(status=ERROR, error="response is not JSON", http_status=code, latency_ms=res.latency_ms)
        if isinstance(body, dict) and "status" in body and "data" in body:
            status = body["status"] if body["status"] in (OK, NOT_FOUND) else ERROR
            return AdapterResponse(
                status=status, data=body.get("data"), error=body.get("error"), http_status=code, latency_ms=res.latency_ms
            )
        return AdapterResponse(status=OK, data=body, http_status=code, latency_ms=res.latency_ms)

    def get_train_status(self, train_number, journey_date=None):
        return self._call("train_status", train_number=train_number, journey_date=journey_date.isoformat() if journey_date else "")

    def get_station_board(self, station_code, window_hours=2):
        return self._call("station_board", station_code=station_code, window_hours=window_hours)

    def get_pnr_status(self, pnr_number):
        return self._call("pnr_status", pnr_number=pnr_number)

    def get_chart_status(self, pnr_number):
        return self._call("chart_status", pnr_number=pnr_number)

    def get_cancelled_trains(self, on_date: date):
        return self._call("cancelled_trains", date=on_date.isoformat())

    def get_rescheduled_trains(self, on_date: date):
        return self._call("rescheduled_trains", date=on_date.isoformat())

    def get_diverted_trains(self, on_date: date):
        return self._call("diverted_trains", date=on_date.isoformat())

    def get_train_schedule(self, train_number):
        return self._call("train_schedule", train_number=train_number)

    def get_station_schedule(self, station_code, window_hours=4):
        return self._call("station_schedule", station_code=station_code, window_hours=window_hours)

    def health_check(self):
        url = self.settings.get("health_url")
        if self.source.get("automated_access") != "permitted":
            return HealthResult(status="down", error="automated access not marked as permitted")
        if not url:
            return HealthResult(status="not_applicable", details={"note": "no health_url configured"})
        res = self.http.get(url, check_robots=self.settings.get("check_robots", True))
        if res.response is None or res.response.status_code >= 400:
            return HealthResult(
                status="down",
                http_status=res.response.status_code if res.response is not None else None,
                latency_ms=res.latency_ms,
                error=res.error,
            )
        return HealthResult(status="up", http_status=res.response.status_code, latency_ms=res.latency_ms)
