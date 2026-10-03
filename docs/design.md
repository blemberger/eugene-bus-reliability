# Design notes

## Matching realtime to schedule

Every realtime row identifies a stop event by `(trip_id, start_date, stop_sequence)`. The static side (`int_scheduled_stop_events`) is keyed the same way, with `start_date` = service date. Matching is therefore an equality join, not a heuristic; the heuristics live in one place, deriving *observed* arrivals from vehicle positions (`int_observed_arrivals`).

Why service date and not feed version on realtime rows: the feed doesn't know which download of the schedule you have. `int_feed_version_by_date` picks, for each date, the most recently downloaded feed version that covers it. A re-published feed therefore applies retroactively to earlier dates it covers, which is the right call (a re-publish is a correction).

## Service-day arithmetic

GTFS `stop_times` are seconds after "noon minus 12 hours" on the service date. Anchoring at noon rather than midnight is what makes DST days come out right; `24:05:00` is 00:05 the following calendar day. The poller's fallback service date (only used if a TripUpdate omits `start_date`, which LTD's doesn't) rolls over at 03:00 local.

## Storage volume, back of the envelope

At 30-second polls with ~100 active vehicles:

- `rt.fetch`: ~2,900 rows/day per feed, trivial.
- `rt.vehicle_position`: ≤ 290k rows/day (fewer, since a vehicle's `position_timestamp` often repeats between polls and is deduplicated). ~100 MB/month with indexes.
- `rt.prediction_*`: bounded by *changes*, not polls. Expect single-digit changes per trip-stop per day, so on the order of 100–500k rows/day. This is the figure `python -m eugene_bus_reliability report` measures; the "how often does a prediction change" section is there to check it.
- Raw archive: ~3 × 2,900 gzipped protobufs/day; a few hundred MB/month. Cheap object storage once it leaves the server.

Storing every poll's predictions verbatim would be ~3–5M rows/day. That is the design choice the change-only scheme exists to avoid.

## What the maintenance script does

`rt.prediction_current` is the upsert target, so it should stay about one service day wide. `sql/maintenance/close_out_stale_predictions.sql` moves rows for service dates older than two days into history. Idempotent; the scheduler (`eugene_bus_reliability/scheduler.py`) runs it daily, together with the schedule reload.

## Definitions

- **On-time**: arrival no more than 60 s early and no more than 300 s late, at timepoints only (TCRP convention).
- **Prediction horizon**: `arrival_time − first_seen_at` for the prediction row in force at a given moment.
- **Prediction error**: `observed_arrival − arrival_time`, signed (positive = bus arrived after the prediction).
- **Observed arrival**: LTD's positions carry no stop fields, so each position is projected onto the trip's route shape (a fraction 0–1 along it), the running maximum removes GPS jitter, and the moment that fraction passes a stop is interpolated between the two reports either side (`int_observed_arrivals`). The feed's own "settled" time for stops already passed is recorded independently and the two are compared (`mart_method_agreement`).

## Known failure modes to measure, not assume

- Feed outages (gaps in `rt.fetch.header_timestamp`)
- Vehicles with stale GPS (`position_timestamp` not advancing while the feed header does)
- Trips cancelled without a `CANCELED` schedule relationship
- Trip ids in the feed that don't match the loaded schedule (the report counts these)

## Later layers, and why they wait

**Question interface.** An LLM that translates "which stop on route 11 is worst at 5 pm on weekdays?" into SQL against the marts, runs it, and answers. It is only as good as the marts it queries and the evaluation set that scores it, so it comes after both exist. The evaluation set — 30–50 questions with known answers computed directly in SQL — is the deliverable; the agent is the demo.

**Delay model.** Needs observed arrivals for ground truth and a few weeks of history for training. LTD's own prediction is the honest baseline; beating "current delay persists" is easy and beating the agency at short horizons is a coin flip because they see the same data. Longer horizons and day-ahead questions are where a learned model can win. Capped at a day; a negative result is published as such.

## Data growth and retention

Measured rates (October 2026, a full weekday): ~100k positions and ~520k superseded
predictions per day, ~550 MB/month of gzipped raw archive, and the database growing by
about 4 GB a month. Unchecked, that fills the server's 48 GB disk in well under a year.

Plan, in order of when it bites:

1. **Raw archive is permanent and cheap.** It stays on the server until it approaches
   half the disk, then moves monthly to object storage (S3/B2, cents per GB per month).
   `make replay` can rebuild any table from it, so the archive is the backup of the
   realtime layer; the schedule reloads from LTD; the analysis layer is recomputed.
2. **`lookback_days` (dbt var, 120) bounds the analysis rebuild.** Older stop events are
   not recomputed each run; when incremental models arrive, they stay in the fact table.
3. **Retention on `rt.vehicle_position` and `rt.prediction_history`** (delete rows older
   than N days after they have been scored and are in the archive) is the lever that keeps
   the database small. Not enabled yet; it becomes necessary at a few months of data on a
   40 GB server.
4. **The scored dataset (`fct_stop_events`) is the product** and is kept indefinitely;
   at ~37k rows/day it is a few GB a year, and it is what the CSV downloads (and a future
   public data release) export.

## Operational risks and their mitigations

- Feed URL or format change → poller logs errors every cycle, the Status page shows fetch
  rate 0 and `make fetch-dump` shows the error lines. Archive continues to hold whatever was
  fetched. Fix the URL in `.env`; nothing else changes.
- Database out of memory or disk → container restarts; on a server, disk is the one to
  watch (the Status page shows database size). Retention above is the fix.
- Duplicate collectors (laptop and server both polling) → harmless for correctness
  (idempotent on header timestamp) but wasteful and confusing; stop one.
- Dashboard SQL box → the app connects as a read-only role with a 15 s statement
  timeout, so it cannot write and cannot hog the database.
- Secrets → `.env` only, never in the image (`.dockerignore`) or the repo (`.gitignore`);
  Postgres bound to localhost; on the server, SSH keys only and only ports 22, 80 and 443 open.