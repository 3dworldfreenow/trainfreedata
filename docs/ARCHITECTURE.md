# Architecture

```
            Browser (Bootstrap dashboard)            other internal services
                     │  session + CSRF                     │  X-Internal-Token
                     ▼                                     ▼
 Apache 2.4 ──► gunicorn ──► Flask app (railmon.web) ── /api/v1 REST
   (TLS, static)             │  views.py (12 pages)        api.py
                             ▼
                      services/ (business logic)
     trains · stations · pnr · chart · disruptions · reports · health
                             │
                             ▼
                 SourceManager (services/sources.py)
   Redis cache ─ single-flight lock ─ per-source rate limit ─ health log
                             │
        ┌──────────────┬─────┴─────────┬──────────────────┬──────────────┐
        ▼              ▼               ▼                  ▼              ▼
 static_timetable  ntes_manual  indianrail_pnr_manual  manual_entry  generic_http
 (MySQL, CC0 data) (manual link) (manual link)         (attribution) (permitted APIs)

 railmon-scheduler (APScheduler worker) ── same services ── monitoring_jobs table
 MySQL: persistent state            Redis: cache · locks · rate limits (all keys expire)
```

## Stack decision

The brief asks for "Python, Flask" and also lists a PHP front end. To keep one
language, one deployment and one security model, the user-facing dashboard,
forms, admin settings and reports are implemented in **Flask (Jinja2 +
Bootstrap 5)**. The internal REST API (`/api/v1`, see `API.md`) is the
contract between the UI and the monitoring back end, so a PHP (or any other)
front end can be added later without touching the Python services.

## Processes

| Process | Command | Role |
|---|---|---|
| `railmon-web` | `gunicorn railmon.wsgi:app` | Dashboard + REST API |
| `railmon-scheduler` | `python -m railmon.scheduler` | Monitoring jobs |
| MySQL 8 | — | All persistent data (connection pool: `pool_pre_ping`, `pool_recycle`) |
| Redis 7 | — | Cache, request de-duplication, rate limiting |

## Request path for any external data

`SourceManager.fetch(capability, **args)`:

1. **Cache** — identical request within the TTL → cached envelope (`cached: true`).
   PNR cache keys use an HMAC of the PNR, never the number.
2. **Single-flight** — Redis lock per request key; concurrent identical
   requests wait for the first and reuse its result.
3. **Adapters in priority order**, only enabled ones that support the capability.
4. **Per-source rate limit** (fixed window, `api_sources.rate_limit_per_minute`)
   for adapters that make network requests. If Redis is down these calls are
   *denied* (fail-safe towards the remote service).
5. **Isolation** — any adapter exception becomes an `error` result and a
   `system_logs` entry; the next adapter is tried.
6. **Health** — each networked call writes `source_health` and updates
   `api_sources.last_status / consecutive_failures`.
7. **Outcome** — first `ok`/`not_found` wins (and is cached); otherwise
   `manual_required` (with official links) or `unavailable` (with reasons).

HTTP layer (`railmon/http.py`): pooled `requests.Session`, retries with
exponential backoff + jitter on connection errors/429/5xx (honours
`Retry-After`), robots.txt enforcement (cached 24 h; unreachable ⇒ not
permitted), identifying User-Agent.

If Redis is unreachable the app keeps working: caching is skipped, locks fall
back to process-local locks, and a 30-second circuit breaker avoids repeated
slow connection attempts.

## Scheduler

* Jobs live in `monitoring_jobs` (seeded from `config/scheduler.yaml`).
* `max_instances=1`, `coalesce=True` and a Redis lock `job:<key>` ⇒ no overlap,
  even with several workers.
* A reconcile loop (15 s) applies dashboard changes: pause/resume, interval or
  cron edits, "run now".
* Jobs that need automated data **skip** (status `skipped`, with the reason)
  when no enabled source with `automated_access = permitted` supports the
  capability — so nothing pointlessly hits manual-only sources.

| Job | Default | Notes |
|---|---|---|
| `train_status` | every 5 min | monitored trains, max 50/run |
| `station_boards` | every 5 min | monitored stations, max 20/run |
| `disruptions` | daily 05:30 IST | today + tomorrow |
| `pnr_checks` | every 10 min | due PNRs only, max `PNR_MAX_CHECKS_PER_RUN`; adaptive per-PNR interval |
| `chart_checks` | every 5 min | due PNRs only; tighter near departure |
| `source_health` | every 30 min | at most one probe per source per 5 min |
| `daily_summary` | daily 23:55 IST | stores `daily_reports` |
| `retention` | daily 03:15 IST | purges expired PNRs, old logs/health/live rows |

### PNR & chart pacing

Per-PNR next check (floor `PNR_MIN_CHECK_INTERVAL_MINUTES`, or a fixed per-PNR interval):
>7 days to departure: 12 h · 2–7 days: 6 h · 12–48 h: 2 h · <12 h: 30 min · >6 h after departure: stop.

Chart checks (floor `CHART_MIN_CHECK_INTERVAL_MINUTES`): first at T-24 h,
then every 2 h until T-6 h, every 30 min until T-2 h, every 15 min until T+2 h;
stop once a source reports *prepared*.

The departure time comes from the static timetable (boarding station + journey
date) and is used **only** for pacing. Chart status changes only when a source
or the user explicitly reports it; `unknown` never overwrites a known value;
each change is stored in `chart_status_history` with its timestamp.

## Data model (MySQL)

See `sql/schema.sql` (generated from `railmon/models.py`; a test fails if it
is stale). Key points:

* `trains`, `stations`, `train_schedules` (timetable versions), `train_routes` (stops per version).
* `train_live_status` and `station_board_snapshots` store a new row **only when
  content changes** (`content_hash`); unchanged re-fetches bump `last_confirmed_at`.
* `train_disruptions` upsert on (train, date, type, source) with `first_seen_at` / `last_seen_at`.
* `pnrs` → `pnr_passengers`, `pnr_status_history`, `chart_status_history` with `ON DELETE CASCADE`.
* `api_sources`, `source_health`, `monitoring_jobs`, `system_logs`, plus `users` and `daily_reports`.
* Every table has indexes for its access paths and timestamps in UTC.

## Privacy & security

* PNR numbers: Fernet-encrypted (`pnr_encrypted`), HMAC-SHA256 lookup hash
  (`pnr_hash`), only the last 4 digits displayed or returned by the API.
* PNR status snapshots in `pnr_status_history` are encrypted.
* Passenger names/ages are dropped during normalisation and never stored.
* PNR pages and endpoints require login; users only see their own PNRs.
* Delete a PNR → all its passengers, history and chart events are removed.
  `retention` purges PNRs `PNR_RETENTION_DAYS_AFTER_JOURNEY` days after travel.
* CSRF tokens on all state-changing requests, session rotation on login,
  login rate limiting, security headers, PBKDF2/scrypt password hashes (Werkzeug).
