# RailMon — Indian Railway Intelligence & Monitoring System

A self-hosted platform (Python · Flask · MySQL · Redis · APScheduler) for
monitoring Indian Railways using **free, legally accessible data only**.

> **Honesty first.** RailMon never fabricates live train or PNR data. Every
> value is labelled **LIVE**, **CACHED**, **STATIC** (published timetable) or
> **MANUAL** (recorded by a user from an official channel). When a source does
> not permit automated access, RailMon links to the official enquiry page
> instead of scraping it.

## What works today (verified 2 Nov 2026 — see [docs/DATA_SOURCES.md](docs/DATA_SOURCES.md))

| Feature | Status |
|---|---|
| All-India train & station master data, routes, timetable search, route search ("trains between"), scheduled station calls | **Works** — imported from the CC0 datameet dataset (8,990 stations, 5,208 trains, 417k stops; c. 2016 snapshot, labelled STATIC) |
| Live running status, live station boards, cancelled/rescheduled/diverted lists | **Manual enquiry fallback** — official source (NTES) has no documented/permitted automated access. Users can record observations; the pipeline (storage, history, delayed list, reports) is ready and starts polling automatically when you enable a source with permitted access |
| PNR status & chart preparation | **Manual enquiry fallback** — official PNR page requires a CAPTCHA (never bypassed). Users record statuses; change detection, encrypted history and timestamped chart tracking work fully |
| Daily reports, searchable disruption history, CSV import of official notices | **Works** with recorded data |
| Source health, scheduler controls, logs | **Works** |

## Features

* 12 dashboard pages: overview, all-India train monitor, train details, station
  board, PNR enquiry, saved-PNR monitor, chart monitor, cancelled/disruptions,
  delayed trains, daily reports, source health, settings & logs.
* Adapter architecture — one adapter per source, normalised JSON envelope
  (`source, fetched_at, status, data, cached, error`), plug-in `generic_http`
  adapter for any free API with documented permission.
* Redis caching, request de-duplication, per-source rate limits, retries with
  exponential backoff, robots.txt enforcement, graceful failure isolation.
* APScheduler worker with pause/resume/run-now from the UI and no overlapping runs.
* PNR privacy: encrypted at rest, masked everywhere, no passenger names, one-click deletion, retention purge.

## Repository layout

```
railmon/            Python package
  adapters/         source adapters (static timetable, NTES/PNR manual, generic HTTP)
  services/         business logic + SourceManager (cache/dedup/rate-limit/health)
  scheduler/        APScheduler worker and job implementations
  importers/        datameet open-data importer
  web/              Flask views, REST API, templates, static assets
config/             sources.yaml, scheduler.yaml
sql/schema.sql      MySQL DDL (generated from models)
deploy/             Apache, systemd, Redis, gunicorn configs
docs/               DATA_SOURCES, INSTALL, ARCHITECTURE, API, TESTING
tests/              pytest suite (no network, no MySQL/Redis needed)
```

## Quick start (development, with uv)

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

```bash
export PATH="$HOME/.local/bin:$PATH"
```

```bash
uv venv --python 3.12 .venv
```

```bash
uv pip install --python .venv -r requirements-dev.txt
```

(Or simply `uv sync`, which uses `pyproject.toml` / `uv.lock`, and prefix commands with `uv run`.)

```bash
.venv/bin/python -m pytest -q
```

```bash
cp .env.example .env && .venv/bin/python -m railmon.genkeys
```

Paste the keys into `.env`. For a quick local try set
`DATABASE_URL=sqlite:///data/railmon.sqlite` (relative to the project root);
use MySQL in production. Redis is optional for a local try (the app degrades
gracefully without it). Then:

```bash
.venv/bin/flask --app railmon.wsgi railmon init-db
```

```bash
.venv/bin/flask --app railmon.wsgi railmon import-datameet
```

```bash
.venv/bin/flask --app railmon.wsgi railmon create-user admin --admin
```

```bash
.venv/bin/flask --app railmon.wsgi run
```

In a second terminal, the scheduler worker:

```bash
.venv/bin/python -m railmon.scheduler
```

Open http://127.0.0.1:5000 and log in.

Production install on Ubuntu 24.04: **[docs/INSTALL.md](docs/INSTALL.md)**.

## Documentation

* [docs/DATA_SOURCES.md](docs/DATA_SOURCES.md) — Phase 1 research, evidence, limitations, adapter output shapes, adding sources
* [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — components, request path, scheduler, data model, privacy
* [docs/API.md](docs/API.md) — internal REST API
* [docs/INSTALL.md](docs/INSTALL.md) — Ubuntu 24.04 deployment
* [docs/TESTING.md](docs/TESTING.md) — automated and failure-mode testing

## Note on the front end

The brief mentions both Flask and a PHP UI. This implementation uses Flask for
the dashboard (single language and deployment); the `/api/v1` REST API is the
boundary, so a PHP front end can be added against it if required.

## Licence of data

Timetable data: datameet/railways, CC0. Official Indian Railways services are
linked, not copied.
