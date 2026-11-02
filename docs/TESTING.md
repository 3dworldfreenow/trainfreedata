# Testing

## Automated tests

The suite uses SQLite in memory (with foreign keys enforced) and `fakeredis`,
so it needs no MySQL/Redis and makes **no network requests** (HTTP is mocked).

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
```

```bash
.venv/bin/python -m pytest -q
```

What is covered (73 tests):

| Area | File | Examples |
|---|---|---|
| Normalisation & privacy | `test_normalize_security.py` | CNF/RAC/WL/GNWL/RLWL/PQWL… parsing, coach/berth, explicit-only chart status, names dropped, IST→UTC, encryption/HMAC |
| Cache, locks, rate limits, HTTP | `test_cache_http.py` | exclusive locks, fixed-window limits, **Redis down** degradation, retries/backoff, give-up, robots.txt allow/deny/unreachable |
| Source failures | `test_sources.py` | manual fallback with official links, caching/dedup (adapter called once), failover past an exception and a 503, all-failed → `unavailable`, **per-source rate limit**, permission gating, generic HTTP adapter, PNR never in Redis keys |
| PNR & chart | `test_pnr_chart.py` | encryption, history + change detection, **chart never inferred**, timestamped chart changes, manual entry, adaptive intervals, cascade delete, retention, per-user enquiry limit |
| Scheduler | `test_scheduler.py` | skip without permitted source, pause, **overlap prevention**, failure recorded not raised, snapshots only on change, disruption upsert |
| Import & search | `test_importer_trains.py` | datameet import + idempotency, schedule versioning, pass-through handling, route search, STATIC labelling, departure derivation, CSV import with bad rows |
| Web & API | `test_web.py` | all pages render, login/CSRF, PNR masking end-to-end, users isolated, admin-only settings, internal token, `sql/schema.sql` up to date |

## Manual / staging checks (Phase 8)

1. **Source reachability from your server** — `flask railmon probe-sources`;
   compare with `docs/DATA_SOURCES.md`. NTES may be reachable from Indian IPs.
2. **Source failure** — stop network access (e.g. `sudo ufw deny out 443`),
   click *Probe now* on *Sources*: statuses go `down` with the reason; pages
   keep working with static/manual data; `system_logs` shows warnings.
3. **Redis failure** — `sudo systemctl stop redis-server`: dashboard still
   loads (`/api/v1/health` shows `"redis": false`); jobs still run; outbound
   source calls are refused (fail-safe). Start Redis again.
4. **Rate limits** — set a source's limit to 1/min in *Settings* and request
   the same capability for different trains; the second call reports the limit.
5. **Overlap** — set a job to 60 s and click *Run now* repeatedly; the log shows
   `job already running elsewhere; skipped` instead of parallel runs.
6. **Pause/resume** — pause `pnr_checks`; within 15 s *Next run* becomes `—`.
7. **Data accuracy** — for a few trains compare the timetable page with the
   official timetable (the open dataset is c. 2016; expect differences — they
   are labelled STATIC). For PNRs, compare recorded statuses with the official page.
8. **Privacy** — `SELECT pnr_encrypted, pnr_hash FROM pnrs;` must not contain
   the PNR digits; delete a PNR and confirm its history rows are gone.

## Regenerating the schema

After changing `railmon/models.py`:

```bash
.venv/bin/python -m railmon.dump_schema > sql/schema.sql
```
