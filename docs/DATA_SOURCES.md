# Data sources — Phase 1 research record

This document records which free sources were investigated, what was actually
observed, whether automated access is permitted, and how RailMon uses each one.
**Verification date: 2 November 2026.** Probes were made from a cloud host
outside India using an identifying User-Agent. Re-run them from your own server
with `flask railmon probe-sources` (results appear on the *Source health* page).

## Summary

| Source | What it offers | Automated access | How RailMon uses it |
|---|---|---|---|
| datameet/railways open dataset | Stations (8,990, with coordinates), trains (5,208), route stops (417,080) | **Permitted** — CC0 licence | Imported into MySQL as the **static** timetable (`static_timetable` source) |
| NTES — National Train Enquiry System (`enquiry.indianrail.gov.in/mntes/`) | Official live running status, live station boards, cancelled / rescheduled / diverted lists | **Not established** — no documented API and no terms granting automation; site reset connections from the probe host | **Manual enquiry fallback** (`ntes` source): deep link + instructions; low-frequency reachability probe only |
| Indian Railways PNR enquiry (`www.indianrail.gov.in/enquiry/PNR/PnrEnquiry.html`) | Official PNR status and chart status | **Not permitted** — form requires a CAPTCHA | **Manual enquiry fallback** (`indianrail_pnr` source); users record what they saw (`manual` source) |
| IRCTC (`www.irctc.co.in`) | Booking, PNR for logged-in users | **Not permitted** — returned `403 Access Denied` even for `robots.txt`; login required | Not used |
| SMS 139 / Rail Madad | PNR & running status by SMS / phone | Not an automatable public API | Mentioned to users as a manual channel |
| Paid / unofficial third-party "railway APIs" | Various | Paid, unofficial, or undocumented redistribution of official data | **Not used.** Any *free, documented, permitted* API can be plugged in with the `generic_http` adapter |

## Evidence per source

### 1. datameet/railways (CC0) — used

* URL: <https://github.com/datameet/railways> — files `stations.json`,
  `trains.json`, `schedules.json` downloaded via `raw.githubusercontent.com`
  (HTTP 200; 1.9 MB, 14.8 MB, 82 MB).
* Licence: CC0 (stated in the repository README) — free to copy, import and redistribute.
* Contents verified on import: 8,990 stations (293 without coordinates),
  5,208 trains, 417,080 schedule rows. Route order follows the `id` field.
* Limitations (all surfaced in the UI):
  * Compiled around 2016 — numbers, timings and routes may have changed.
  * **No running days**; `trains.running_days` stays `NULL` and the UI says "Unknown".
  * Schedules include pass-through points where arrival equals departure;
    these are imported with `is_halt = false` and excluded from route search and station boards.
  * Some station codes appear only in train/schedule files; these are created
    with `data_source = 'datameet:inferred'`.
* RailMon label: **STATIC**. It is never shown as live data.

### 2. NTES — not automated

* Landing page `https://enquiry.indianrail.gov.in/mntes/` and `robots.txt`:
  TLS connection reset by peer from the probe host (HTTP via the plain-text
  port returned 503 "upstream connect error"). The service is commonly
  restricted to Indian networks; from an Indian server it may load.
* No public API documentation, developer terms or licence for programmatic use
  were found. Without documented permission, RailMon treats automation as
  **not permitted** and does not scrape NTES pages or call its internal
  endpoints.
* The adapter (`ntes_manual`) returns `manual_required` for
  `get_train_status`, `get_station_board`, `get_cancelled_trains`,
  `get_rescheduled_trains`, `get_diverted_trains` with the official link and
  what to look up. Users may record what they see (labelled **MANUAL**).
* Health probe: one GET of the landing page at most every 5 minutes (default
  job interval 30 min), only if `robots.txt` can be fetched and allows it. An
  unreachable `robots.txt` is treated as "not permitted" (status `down`, reason shown).

### 3. Official PNR enquiry — not automated

* `robots.txt` at `www.indianrail.gov.in` returned `User-agent: * / Disallow:` (crawling allowed).
* The PNR page (HTTP 200) contains a CAPTCHA question/answer field
  (`CaptchaQuestionLabel`, "Captcha should not be blank"). **CAPTCHAs are
  never bypassed**, so PNR status cannot be fetched automatically.
* The host offers only TLS 1.2 RSA-key-exchange AES-GCM cipher suites, which
  Python's default OpenSSL cipher list excludes. RailMon appends those suites
  (`HTTP_EXTRA_CIPHERS`) so the reachability probe works; certificate
  verification is unchanged.
* Health probe reports `manual_only` and records whether a CAPTCHA is present.
* PNR workflow: the user opens the official page (link shown), then records
  booking status, current status and chart status in RailMon. History and
  change detection work identically for manual and automated data.

### 4. IRCTC — not used

* `https://www.irctc.co.in/robots.txt` → `403 Access Denied` (Akamai). Booking
  and PNR features require login. Not suitable for automation.

## Data kinds shown in the UI

| Label | Meaning |
|---|---|
| **LIVE** | Fetched just now from a source whose automated access is `permitted` |
| **CACHED** | An identical recent result reused from Redis (within the TTL) |
| **STATIC** | Published/open timetable data — not real-time |
| **MANUAL** | Entered by a signed-in user from an official channel |

When no data exists, pages say so (e.g. "no status data", "chart status
unknown") instead of showing placeholders.

## Normalised adapter output

Every adapter call is wrapped into:

```json
{
  "source": "ntes",
  "fetched_at": "2026-11-02T04:45:40Z",
  "status": "ok | not_found | manual_required | unavailable | error | rate_limited | disabled",
  "data": { ... } ,
  "cached": false,
  "error": null,
  "data_kind": "live | static | manual",
  "manual_enquiry": [{"source": "...", "name": "...", "url": "...", "instructions": "...", "reason": "..."}]
}
```

`data` shapes (fields may be `null` when the source does not provide them):

* **train_status**: `train_number, train_name, journey_date, running_status, current_station_code,
  last_reported_station_code, last_reported_at, next_station_code, delay_minutes, eta, etd,
  stations[] {code, name, sch_arr, sch_dep, act_arr, act_dep, delay_minutes, status}`
* **station_board**: `station_code, window_hours, entries[] {train_number, train_name, sch_arr, sch_dep,
  exp_arr, exp_dep, delay_minutes, platform, status}`
* **pnr_status**: `train_number, train_name, journey_date, from_station_code, to_station_code,
  boarding_station_code, travel_class, chart_status, passengers[] {serial, booking_status, current_status, coach, berth}`
  (any name/age fields are dropped during normalisation)
* **chart_status**: `chart_status` (`"Chart Prepared"`, `"Chart Not Prepared"`, booleans; anything else → `unknown`)
* **cancelled/rescheduled/diverted_trains**: `trains[] {train_number, train_name, disruption_type?, from_station_code,
  to_station_code, affected_from_code, affected_to_code, rescheduled_departure, rescheduled_by_minutes, diverted_route, reason}`
* Datetimes are ISO-8601; naive values are interpreted as IST.

## Adding a new free source

1. Confirm its terms permit automated access (and note rate limits).
2. Either configure the built-in `generic_http` adapter (Settings → set
   endpoints in `api_sources.config`, then `automated_access = permitted`), or
   write an adapter:

```python
from railmon.adapters import register_adapter
from railmon.adapters.base import BaseAdapter, AdapterResponse, OK

@register_adapter
class MyAdapter(BaseAdapter):
    adapter_name = "my_source"
    capabilities = {"train_status"}

    def get_train_status(self, train_number, journey_date=None):
        res = self.http.get(f"https://api.example.org/trains/{train_number}")
        ...
        return AdapterResponse(status=OK, data={...normalised...}, http_status=200, latency_ms=res.latency_ms)
```

3. Put the module on the Python path and list it in `ADAPTER_MODULES` in `.env`
   (e.g. `ADAPTER_MODULES=my_adapters`). Add the source to `config/sources.yaml`, run `flask railmon sync-sources`, and enable it.
   Scheduled jobs start polling automatically once an enabled source with
   `automated_access = permitted` supports the capability.
