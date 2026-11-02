# Internal REST API (`/api/v1`)

Authentication:

* **Browser session** (after `/login`). Write requests need the `X-CSRF-Token`
  header (value from the page's `<meta name="csrf-token">`).
* **Service token**: header `X-Internal-Token: <INTERNAL_API_TOKEN>` — acts as
  the first admin user, CSRF not required. Keep it on internal networks only.

Public endpoints honour `LOGIN_REQUIRED_FOR_PUBLIC_PAGES`. Errors are
`{"status": "error", "error": "..."}` with the HTTP status code.

Source-backed endpoints return the normalised envelope described in
`DATA_SOURCES.md` (`source, fetched_at, status, data, cached, error, data_kind, manual_enquiry`).

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/health` | none | DB/Redis liveness (503 if DB down) |
| GET | `/stats` | public | Dashboard counters, source status, freshness |
| GET | `/suggest/stations?q=nd` | public | Station autocomplete (≥ 2 chars; code or name; ranked) |
| GET | `/suggest/trains?q=129` | public | Train autocomplete (number prefix or name) |
| GET | `/trains?q=&station=&type=&sort=&dir=&page=&per_page=` | public | Search master data (+ latest stored status) |
| GET | `/trains/between?from=NDLS&to=BCT` | public | Route search on the static timetable |
| GET | `/trains/{number}` | public | Train, static schedule envelope, latest status |
| GET | `/trains/{number}/status?date=YYYY-MM-DD` | public | Running status via adapters (stored on change) |
| GET | `/trains/{number}/history` | public | Stored status snapshots |
| GET | `/delayed?min=15&date=` | public | Latest statuses with delay ≥ min |
| GET | `/stations?q=&page=` | public | Station search |
| GET | `/stations/{code}/board?hours=2` | public | Live board envelope, static scheduled calls, last snapshot |
| GET | `/disruptions?type=&from=&to=&q=&page=` | public | Disruption history search |
| POST | `/disruptions/refresh?date=` | admin | Refresh disruption reports now |
| GET | `/reports/daily?date=` | public | Stored + freshly computed daily summary |
| POST | `/pnr/enquiry` `{"pnr": "1234567890"}` | user | One-off PNR enquiry (rate-limited per user) |
| GET | `/pnrs` | user | Your saved PNRs (masked) |
| POST | `/pnrs` `{"pnr", "label", "journey_date", "train_number", "boarding_station_code", "check_interval_minutes"}` | user | Save PNR for monitoring |
| GET | `/pnrs/{id}` | owner/admin | PNR with decrypted history and chart events |
| POST | `/pnrs/{id}/check` | owner/admin | Check now (bypasses cache) |
| POST | `/pnrs/{id}/manual-status` `{"p1_booking", "p1_current", …, "chart_status"}` | owner/admin | Record status from an official channel |
| DELETE | `/pnrs/{id}` | owner/admin | Delete PNR and all its history |
| GET | `/sources` | public | Sources, capabilities, health, 24 h availability |
| GET | `/jobs` | admin | Monitoring jobs |
| POST | `/jobs/{key}/pause\|resume\|run` | admin | Control a job |

Example (service token):

```bash
curl -s -H "X-Internal-Token: $INTERNAL_API_TOKEN" -X POST http://127.0.0.1:8000/api/v1/jobs/pnr_checks/pause
```
