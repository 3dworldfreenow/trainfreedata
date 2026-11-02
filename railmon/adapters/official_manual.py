"""Adapters for official Indian Railways enquiry services.

Research summary (see docs/DATA_SOURCES.md for the full record):

* NTES (https://enquiry.indianrail.gov.in/mntes/) - the official running-status,
  station-board and exceptional-train (cancelled/rescheduled/diverted) service.
  It publishes **no documented public API** and no terms granting automated
  access. During verification it reset connections from non-Indian cloud IPs.
  We therefore do not scrape it: these adapters return ``manual_required`` with
  the official link, and only perform a low-frequency reachability probe of the
  public landing page (subject to robots.txt).

* Indian Railways PNR enquiry (https://www.indianrail.gov.in/enquiry/PNR/PnrEnquiry.html)
  - robots.txt allows crawling, but the enquiry form is protected by a CAPTCHA.
  Bypassing a CAPTCHA is not permitted, so PNR status must be checked manually;
  users can then record what they saw (stored as ``manual`` data).

If Indian Railways / CRIS later publish a permitted API, implement it as a new
adapter (or configure the ``generic_http`` adapter) - nothing else changes.
"""
from __future__ import annotations

import re

from .base import MANUAL_REQUIRED, AdapterResponse, BaseAdapter, HealthResult

NTES_URL = "https://enquiry.indianrail.gov.in/mntes/"
PNR_URL = "https://www.indianrail.gov.in/enquiry/PNR/PnrEnquiry.html?locale=en"


def _probe(http, url: str, *, captcha_check: bool = False) -> HealthResult:
    from ..http import RobotsDisallowed

    try:
        res = http.get(url)
    except RobotsDisallowed as exc:
        return HealthResult(status="down", error=str(exc), details={"robots": "disallowed_or_unreachable"})
    if res.response is None:
        return HealthResult(status="down", latency_ms=res.latency_ms, error=res.error)
    code = res.response.status_code
    if code >= 400:
        return HealthResult(status="down", http_status=code, latency_ms=res.latency_ms, error=res.error)
    details = {}
    if captcha_check:
        details["captcha_present"] = bool(re.search(r"captcha", res.response.text or "", re.I))
    # Reachable, but automated data retrieval is not permitted/possible -> manual only.
    return HealthResult(status="manual_only", http_status=code, latency_ms=res.latency_ms, details=details)


class NtesManualAdapter(BaseAdapter):
    adapter_name = "ntes_manual"
    manual_only = True
    capabilities = {"train_status", "station_board", "cancelled_trains", "rescheduled_trains", "diverted_trains"}

    def _manual(self, what: str) -> AdapterResponse:
        url = self.source.get("base_url") or NTES_URL
        return AdapterResponse(
            status=MANUAL_REQUIRED,
            data_kind="manual",
            network=False,
            manual_url=url,
            manual_instructions=f"Open NTES and look up {what}. NTES offers no permitted automated access.",
            error="NTES does not provide a documented/permitted automated interface",
        )

    def get_train_status(self, train_number, journey_date=None):
        return self._manual(f"'Spot Your Train' for train {train_number}")

    def get_station_board(self, station_code, window_hours=2):
        return self._manual(f"'Live Station' for station {station_code}")

    def get_cancelled_trains(self, on_date):
        return self._manual(f"'Exceptional Trains > Cancelled Trains' for {on_date:%d-%m-%Y}")

    def get_rescheduled_trains(self, on_date):
        return self._manual(f"'Exceptional Trains > Rescheduled Trains' for {on_date:%d-%m-%Y}")

    def get_diverted_trains(self, on_date):
        return self._manual(f"'Exceptional Trains > Diverted Trains' for {on_date:%d-%m-%Y}")

    def health_check(self):
        if not self.settings.get("probe", True):
            return HealthResult(status="manual_only", details={"probe": "disabled"})
        return _probe(self.http, self.source.get("base_url") or NTES_URL)


class IndianRailPnrManualAdapter(BaseAdapter):
    adapter_name = "indianrail_pnr_manual"
    manual_only = True
    capabilities = {"pnr_status", "chart_status"}

    def _manual(self) -> AdapterResponse:
        return AdapterResponse(
            status=MANUAL_REQUIRED,
            data_kind="manual",
            network=False,
            manual_url=self.source.get("base_url") or PNR_URL,
            manual_instructions=(
                "Enter the PNR on the official Indian Railways PNR enquiry page (CAPTCHA required), "
                "then record the booking status, current status and chart status here. "
                "You can also send 'PNR <number>' by SMS to 139."
            ),
            error="Official PNR enquiry requires a CAPTCHA; automated access is not permitted",
        )

    def get_pnr_status(self, pnr_number):
        return self._manual()

    def get_chart_status(self, pnr_number):
        return self._manual()

    def health_check(self):
        if not self.settings.get("probe", True):
            return HealthResult(status="manual_only", details={"probe": "disabled"})
        return _probe(self.http, self.source.get("base_url") or PNR_URL, captcha_check=True)


class ManualEntryAdapter(BaseAdapter):
    """Attribution-only source for statuses typed in by users from official channels."""

    adapter_name = "manual_entry"
    manual_only = True
    capabilities: set[str] = set()

    def health_check(self):
        return HealthResult(status="not_applicable")
