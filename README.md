# Eugene Bus Watch

[![ci](https://github.com/blemberger/eugene-bus-reliability/actions/workflows/ci.yml/badge.svg)](https://github.com/blemberger/eugene-bus-reliability/actions/workflows/ci.yml)

**Live site: [eugenebuswatch.com](https://eugenebuswatch.com)**

How reliable are **Lane Transit District**'s buses (Eugene–Springfield, Oregon), and how far can you trust the arrival countdown? This project records LTD's public GTFS schedule and GTFS-Realtime feeds every 30 seconds, around the clock, and measures both.

- **On-time performance** by route, stop and hour, from arrivals derived from the buses' own GPS positions.
- **Prediction accuracy**: every prediction LTD publishes is kept, with every revision, and scored against when the bus actually arrived, as a function of how far ahead it was made.
- **An "honest countdown"**: LTD's live prediction corrected by how far off it has usually been for that route, time of day and horizon.

Collecting since September 26, 2026. The analysis updates every 15 minutes; summaries over all history (typical lateness by route, stop and hour; prediction accuracy) at most hourly.

## How it works

```
LTD feeds ──► poller (Python, every 30 s) ──► Postgres/PostGIS ──► dbt models (every 15 min) ──► Streamlit site
                 │                              gtfs.*  rt.*        staging → intermediate → marts
                 └─► raw archive (.pb.gz), the replayable source of truth
```

Everything runs with Docker Compose on one small server, behind Caddy for HTTPS.

## What's here

```
eugene_bus_reliability/  Python: realtime poller, schedule loader, scheduler loop, replay, diagnostics
sql/schema/              Postgres + PostGIS tables: versioned schedule (gtfs.*), realtime layer (rt.*), roles
sql/migrations/          changes for databases that already exist (`make migrate`)
dbt/                     the analysis: staging → intermediate (observed arrivals) → marts, with tests
app/                     the Streamlit site: one script per page in app/views/
tests/                   unit tests on parsing; integration tests on the change-only storage
docker-compose.yml       database, poller, scheduler, site and Caddy
Caddyfile                HTTPS reverse proxy for the site
docs/                    design notes and the deployment guide
```

## Design decisions

**Predictions are stored change-only.** The feed re-asserts every stop-time prediction for every active trip on every update. Storing each poll verbatim is ~4× the volume and carries no information. Instead `rt.prediction_current` holds one row per trip-stop with `[first_seen_at, last_seen_at]` — the interval over which the feed asserted that value — and a change closes the old row into `rt.prediction_history`. The full revision history of every prediction is recoverable; identical polls cost nothing.

**Polls that don't advance the feed's header timestamp are discarded** (counted in `rt.fetch_unchanged` so the poll cadence can be tuned to the feed's real update rate).

**Raw bytes are archived** as gzipped protobufs, one per stored fetch, so the database is rebuildable from source if the parsing logic changes (`make rebuild`).

**Arrivals are measured, not taken from the feed.** LTD's vehicle positions carry no stop information, so each position is placed along the route's shape (walked in time order, so routes that use a street twice don't confuse it) and the moment a bus passes each stop is interpolated. The feed's own "settled" times are recorded independently and the two methods are compared on the site.

**The schedule is versioned.** LTD republishes GTFS a few times a year. Every static table carries `feed_version_id`; realtime rows carry a service date; `int_feed_version_by_date` resolves which schedule applies to each day.

**Postgres only, and a plain scheduler.** ~70 buses reporting every 30 seconds does not need Kafka, Spark or a lakehouse. A small Python loop runs `dbt build` every 15 minutes and the daily jobs; cron would also do.

More in [docs/design.md](docs/design.md).

## Run it locally

Requires Docker and Python 3.12+.

```bash
cp .env.example .env            # set the two passwords
make setup                      # venv with the dev and app dependencies
source .venv/bin/activate
make up                         # Postgres/PostGIS; schema applied on first start
make migrate                    # sets the read-only site user's password
make load-static                # download and load the current LTD schedule
make run                        # poller and scheduler in Docker, continuously
make app                        # the site at http://localhost:8501
make test                       # unit tests (no database)
make test-all                   # + integration tests against the compose test database
```

`make` on its own lists every command. `make dump` writes a diagnostic snapshot of the whole pipeline and every page to `dump.txt`.

Running it on a server, with the site on its own domain: [docs/deploy.md](docs/deploy.md).

## Data sources

- Static GTFS: `https://feed.ltd.org/TMGTFSRealTimeWebService/GTFS/google_transit.zip`
- GTFS-Realtime: TripUpdates, VehiclePositions and Alerts under `https://feed.ltd.org/TMGTFSRealTimeWebService/` (public, no key)

Lane Transit District is the source of all schedule and realtime data. This project is independent and not affiliated with LTD.

## License

MIT for the code. The data is LTD's.