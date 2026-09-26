# LTD Transit Reliability

[![ci](https://github.com/blemberger/eugene-bus-reliability/actions/workflows/ci.yml/badge.svg)](https://github.com/blemberger/eugene-bus-reliability/actions/workflows/ci.yml)

A warehouse and analysis of bus reliability for **Lane Transit District** (Eugene–Springfield, Oregon), built from LTD's public GTFS schedule and GTFS-Realtime feeds.

The headline question is **how accurate is the arrival prediction shown to riders, as a function of how far out it is made.** TripUpdates are the agency's predictions; VehiclePositions are what actually happened. Recording both continuously, and every revision of every prediction, makes that a measurable calibration curve. On-time performance by route, stop and hour, and headway adherence on the frequent routes, come from the same data.

> **Status (September 2026):** collection layer complete and running; analysis layer in progress. Results will be posted here as the history accumulates. Nothing below is a finding yet.

## What's here

```
sql/schema/        Postgres + PostGIS DDL: versioned static schedule (gtfs.*), realtime layer (rt.*)
ltdwatch/          Python: static GTFS loader, realtime poller, CLI
dbt/               Staging → intermediate models with tests (marts to come)
tests/             Unit tests on parsing; integration tests on the change-only upsert
docker-compose.yml Postgres/PostGIS + the poller
```

## Design decisions

**Predictions are stored change-only.** The feed re-asserts every stop-time prediction for every active trip on every update. Storing each poll verbatim is ~10× the volume and carries no information. Instead `rt.prediction_current` holds one row per trip-stop with `[first_seen_at, last_seen_at]` — the interval over which the feed asserted that value — and a change closes the old row into `rt.prediction_history`. The full revision history of every prediction is recoverable; identical polls cost nothing.

**Polls that don't advance the feed's header timestamp are discarded** (counted in `rt.fetch_unchanged` so the poll cadence can be tuned to the feed's real update rate).

**Raw bytes are archived** as gzipped protobufs, one per stored fetch, so the database is rebuildable from source if the parsing logic changes.

**The schedule is versioned.** LTD republishes GTFS about three times a year. Every static table carries `feed_version_id`; realtime rows carry a service date; `int_feed_version_by_date` resolves which schedule applies to each day.

**Postgres only.** ~100 vehicles reporting every 30 seconds does not need Kafka, Spark or a lakehouse. Airflow will be added for orchestration because it's the standard for this job; cron would also do, and the DAG will say so.

More in [docs/design.md](docs/design.md).

## Run it locally

Requires Docker and Python 3.11+.

```bash
cp .env.example .env            # set POSTGRES_PASSWORD
make setup                      # venv + dev dependencies
make up                         # Postgres/PostGIS; schema applied on first start
source .venv/bin/activate
make load-static                # download and load the current LTD schedule
make poll-once                  # 10 poll cycles in the foreground, then a report
make app                        # local dashboard at http://localhost:8501
make poll                       # ...then run the poller continuously in Docker
make test                       # unit tests (no DB)
make test-all                   # + integration tests against the compose DB
make dbt-deps && make dbt-build # dbt models and tests (the scheduler repeats this every 15 min)
```

`python -m ltdwatch report` prints what has been collected: fetch counts, the feed's measured update cadence, how often predictions change, and how many realtime trip ids fail to match the loaded schedule.

## Data sources

- Static GTFS: https://www.ltd.org/files/library/ltdgtfs.zip
- GTFS-Realtime: TripUpdates, VehiclePositions and Alerts under `https://feed.ltd.org/TMGTFSRealTimeWebService/` (public, no key)

Lane Transit District is the source of all schedule and realtime data. This project is not affiliated with LTD.

## Status and next

Collecting since September 18, 2026. The collector, the analysis layer (geometric arrival
derivation cross-checked against the feed's own settled times), and the dashboard are
running. Next: hosted deployment, then a public data release and the write-up of the
prediction-calibration findings.

## License

MIT. Data is LTD's.