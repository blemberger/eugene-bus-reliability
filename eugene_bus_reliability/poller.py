"""Continuous GTFS-Realtime poller: fetch, archive raw bytes, write to Postgres.

Runs forever (docker compose restarts it if it dies). One cycle fetches trip
updates and vehicle positions; alerts every ALERT_EVERY cycles. A fetch whose
feed header timestamp has not advanced since the last one is counted in
rt.fetch_unchanged and otherwise discarded.
"""

from __future__ import annotations

import gzip
import hashlib
import logging
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import psycopg
from psycopg.types.json import Jsonb

from eugene_bus_reliability import USER_AGENT, rt_parse
from eugene_bus_reliability.config import Settings
from eugene_bus_reliability.db import copy_rows

log = logging.getLogger(__name__)

LOCAL_TZ = ZoneInfo("America/Los_Angeles")
ALERT_EVERY = 10  # cycles


PREDICTION_COLUMNS = (
    "trip_id",
    "start_date",
    "stop_sequence",
    "stop_id",
    "arrival_time",
    "departure_time",
    "arrival_delay",
    "departure_delay",
    "schedule_relationship",
    "scheduled_time",
    "first_seen_at",
    "last_seen_at",
    "first_fetch_id",
)
VEHICLE_COLUMNS = (
    "fetch_id",
    "vehicle_id",
    "position_timestamp",
    "trip_id",
    "route_id",
    "start_date",
    "latitude",
    "longitude",
    "bearing",
    "speed_mps",
    "stop_id",
    "current_stop_sequence",
    "current_status",
    "occupancy_status",
    "occupancy_percentage",
)
TRIP_UPDATE_COLUMNS = (
    "fetch_id",
    "trip_id",
    "start_date",
    "route_id",
    "vehicle_id",
    "schedule_relationship",
    "trip_timestamp",
    "trip_delay_seconds",
)

# Change-only upsert for stop-time predictions. The value columns that define "the
# same prediction" are (stop_id, arrival_time, departure_time, arrival_delay,
# departure_delay, schedule_relationship); a change in any of them closes the old
# row into history.

_PRED_COLS = ", ".join(PREDICTION_COLUMNS)
_PRED_COLS_C = ", ".join("c." + col for col in PREDICTION_COLUMNS)
SQL_CLOSE_OUT_CHANGED = f"""
INSERT INTO rt.prediction_history ({_PRED_COLS}, closed_at)
SELECT {_PRED_COLS_C}, t.first_seen_at
FROM rt.prediction_current c
JOIN tmp_pred t USING (trip_id, start_date, stop_sequence)
WHERE (c.stop_id, c.arrival_time, c.departure_time, c.arrival_delay, c.departure_delay,
       c.schedule_relationship)
      IS DISTINCT FROM
      (t.stop_id, t.arrival_time, t.departure_time, t.arrival_delay, t.departure_delay,
       t.schedule_relationship)
  AND c.last_seen_at < t.first_seen_at
ON CONFLICT DO NOTHING
"""

SQL_UPSERT_CURRENT = f"""
INSERT INTO rt.prediction_current AS pc ({_PRED_COLS})
SELECT {_PRED_COLS} FROM tmp_pred
ON CONFLICT (trip_id, start_date, stop_sequence) DO UPDATE SET
    stop_id               = EXCLUDED.stop_id,
    arrival_time          = EXCLUDED.arrival_time,
    departure_time        = EXCLUDED.departure_time,
    arrival_delay         = EXCLUDED.arrival_delay,
    departure_delay       = EXCLUDED.departure_delay,
    schedule_relationship = EXCLUDED.schedule_relationship,
    scheduled_time        = EXCLUDED.scheduled_time,
    last_seen_at          = EXCLUDED.last_seen_at,
    -- keep the original first_seen/first_fetch when the value is unchanged
    first_seen_at = CASE
        WHEN (pc.stop_id, pc.arrival_time, pc.departure_time, pc.arrival_delay,
              pc.departure_delay, pc.schedule_relationship)
             IS DISTINCT FROM
             (EXCLUDED.stop_id, EXCLUDED.arrival_time, EXCLUDED.departure_time,
              EXCLUDED.arrival_delay, EXCLUDED.departure_delay, EXCLUDED.schedule_relationship)
        THEN EXCLUDED.first_seen_at ELSE pc.first_seen_at END,
    first_fetch_id = CASE
        WHEN (pc.stop_id, pc.arrival_time, pc.departure_time, pc.arrival_delay,
              pc.departure_delay, pc.schedule_relationship)
             IS DISTINCT FROM
             (EXCLUDED.stop_id, EXCLUDED.arrival_time, EXCLUDED.departure_time,
              EXCLUDED.arrival_delay, EXCLUDED.departure_delay, EXCLUDED.schedule_relationship)
        THEN EXCLUDED.first_fetch_id ELSE pc.first_fetch_id END
-- only a newer message may update the stored row (see SQL_CLOSE_OUT_CHANGED)
WHERE pc.last_seen_at < EXCLUDED.last_seen_at
"""


def service_date_for(now_utc: datetime):
    """Fallback service date when a TripUpdate omits start_date: local date,
    with the day rolling over at 03:00 so late-night trips stay on the
    previous service day. LTD sets start_date, so this is rarely used."""
    local = now_utc.astimezone(LOCAL_TZ) - timedelta(hours=3)
    return local.date()


class Poller:
    def __init__(self, settings: Settings):
        self.s = settings
        self.last_header: dict[str, datetime | None] = {
            "trip_updates": None,
            "vehicle_positions": None,
            "alerts": None,
        }
        self.urls = {
            "trip_updates": settings.trip_updates_url,
            "vehicle_positions": settings.vehicle_positions_url,
            "alerts": settings.alerts_url,
        }
        self.client = httpx.Client(timeout=20, headers={"User-Agent": USER_AGENT})

    # ----- one feed ---------------------------------------------------------

    def fetch_one(self, conn: psycopg.Connection, feed: str) -> str:
        """Fetch one feed and write it. Returns 'stored' | 'unchanged' | 'error'."""
        fetched_at = datetime.now(tz=UTC)
        try:
            r = self.client.get(self.urls[feed])
            r.raise_for_status()
            data = r.content
            msg = rt_parse.parse_feed(data)
        except Exception as exc:  # network or parse failure: log and move on
            log.warning("%s: fetch failed: %s", feed, exc)
            return "error"

        header_ts = (
            datetime.fromtimestamp(msg.header.timestamp, tz=UTC)
            if msg.header.timestamp
            else fetched_at
        )
        if header_ts == self.last_header[feed]:
            self._count_unchanged(conn, feed, fetched_at)
            return "unchanged"

        try:
            archive_path = self._archive(feed, header_ts, data)
            stored = self.store(conn, feed, data, msg, header_ts, fetched_at, archive_path)
        except Exception:
            log.exception("%s: write failed", feed)
            return "error"
        self.last_header[feed] = header_ts
        if not stored:
            self._count_unchanged(conn, feed, fetched_at)
            return "unchanged"
        return "stored"

    def store(
        self,
        conn: psycopg.Connection,
        feed: str,
        data: bytes,
        msg,
        header_ts: datetime,
        fetched_at: datetime,
        archive_path: str | None,
    ) -> bool:
        """Write one feed message to the database. Returns False if that header
        timestamp was already stored. Used by the live poller and by `replay`."""
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO rt.fetch
                        (feed, fetched_at, header_timestamp, entity_count, byte_size, sha256, archive_path)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (feed, header_timestamp) DO NOTHING
                    RETURNING fetch_id
                    """,
                    (
                        feed,
                        fetched_at,
                        header_ts,
                        len(msg.entity),
                        len(data),
                        hashlib.sha256(data).digest(),
                        archive_path,
                    ),
                )
                row = cur.fetchone()
            if row is None:  # seen before (e.g. after a restart)
                return False
            fetch_id = row[0]
            if feed == "vehicle_positions":
                self._write_vehicle_positions(conn, fetch_id, msg, header_ts)
            elif feed == "trip_updates":
                self._write_trip_updates(conn, fetch_id, msg, header_ts)
            elif feed == "alerts":
                self._write_alerts(conn, fetch_id, msg)
        return True

    # ----- writers -------------------------------------------------------------

    def _write_vehicle_positions(self, conn, fetch_id, msg, header_ts):
        rows = rt_parse.vehicle_positions(msg, fallback_ts=header_ts)
        with conn.cursor() as cur:
            cur.execute(
                "DROP TABLE IF EXISTS tmp_vp; "
                "CREATE TEMP TABLE tmp_vp (LIKE rt.vehicle_position INCLUDING DEFAULTS) ON COMMIT DROP"
            )
            cur.execute("ALTER TABLE tmp_vp DROP COLUMN geom")
        copy_rows(
            conn,
            "tmp_vp",
            VEHICLE_COLUMNS,
            (
                (
                    fetch_id,
                    r.vehicle_id,
                    r.position_timestamp,
                    r.trip_id,
                    r.route_id,
                    r.start_date,
                    r.latitude,
                    r.longitude,
                    r.bearing,
                    r.speed_mps,
                    r.stop_id,
                    r.current_stop_sequence,
                    r.current_status,
                    r.occupancy_status,
                    r.occupancy_percentage,
                )
                for r in rows
            ),
        )
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO rt.vehicle_position ({", ".join(VEHICLE_COLUMNS)})
                SELECT DISTINCT ON (vehicle_id, position_timestamp) {", ".join(VEHICLE_COLUMNS)}
                FROM tmp_vp
                ON CONFLICT (vehicle_id, position_timestamp) DO NOTHING
                """
            )
            log.info("vehicle_positions: %d in feed, %d new", len(rows), cur.rowcount)

    def _write_trip_updates(self, conn, fetch_id, msg, header_ts):
        trips, preds = rt_parse.trip_updates(msg, fallback_start_date=service_date_for(header_ts))
        with conn.cursor() as cur:
            cur.execute(
                "DROP TABLE IF EXISTS tmp_tu; CREATE TEMP TABLE tmp_tu (LIKE rt.trip_update) ON COMMIT DROP"
            )
            cur.execute(
                "DROP TABLE IF EXISTS tmp_pred; CREATE TEMP TABLE tmp_pred (LIKE rt.prediction_current) ON COMMIT DROP"
            )
        copy_rows(
            conn,
            "tmp_tu",
            TRIP_UPDATE_COLUMNS,
            (
                (
                    fetch_id,
                    t.trip_id,
                    t.start_date,
                    t.route_id,
                    t.vehicle_id,
                    t.schedule_relationship,
                    t.trip_timestamp,
                    t.trip_delay_seconds,
                )
                for t in trips
            ),
        )
        copy_rows(
            conn,
            "tmp_pred",
            PREDICTION_COLUMNS,
            (
                (
                    p.trip_id,
                    p.start_date,
                    p.stop_sequence,
                    p.stop_id,
                    p.arrival_time,
                    p.departure_time,
                    p.arrival_delay,
                    p.departure_delay,
                    p.schedule_relationship,
                    p.scheduled_time,
                    header_ts,
                    header_ts,
                    fetch_id,
                )
                for p in preds
            ),
        )
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO rt.trip_update ({", ".join(TRIP_UPDATE_COLUMNS)})
                SELECT DISTINCT ON (fetch_id, trip_id) {", ".join(TRIP_UPDATE_COLUMNS)} FROM tmp_tu
                ON CONFLICT DO NOTHING
                """
            )
            # Duplicate (trip, date, seq) inside one feed message would make the
            # upsert ambiguous; keep the first occurrence.
            cur.execute(
                """
                DELETE FROM tmp_pred a USING tmp_pred b
                WHERE a.trip_id = b.trip_id AND a.start_date = b.start_date
                  AND a.stop_sequence = b.stop_sequence AND a.ctid > b.ctid
                """
            )
            cur.execute(SQL_CLOSE_OUT_CHANGED)
            n_changed = cur.rowcount
            cur.execute(SQL_UPSERT_CURRENT)
            log.info(
                "trip_updates: %d trips, %d predictions, %d changed values",
                len(trips),
                len(preds),
                n_changed,
            )

    def _write_alerts(self, conn, fetch_id, msg):
        rows = rt_parse.alerts(msg)
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO rt.alert (fetch_id, alert_id, payload) VALUES (%s, %s, %s)"
                " ON CONFLICT DO NOTHING",
                [(fetch_id, aid, Jsonb(payload)) for aid, payload in rows],
            )
        log.info("alerts: %d", len(rows))

    def _count_unchanged(self, conn, feed, fetched_at):
        hour = fetched_at.replace(minute=0, second=0, microsecond=0)
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO rt.fetch_unchanged (feed, hour, unchanged_count) VALUES (%s, %s, 1)
                ON CONFLICT (feed, hour) DO UPDATE
                SET unchanged_count = rt.fetch_unchanged.unchanged_count + 1
                """,
                (feed, hour),
            )

    def _archive(self, feed: str, header_ts: datetime, data: bytes) -> str:
        rel = Path(feed) / header_ts.strftime("%Y/%m/%d") / f"{int(header_ts.timestamp())}.pb.gz"
        path = self.s.raw_archive_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(path, "wb") as f:
            f.write(data)
        return str(rel)

    # ----- loop ---------------------------------------------------------------

    def run(self, max_cycles: int | None = None) -> None:
        interval = self.s.poll_interval_seconds
        cycle = 0
        log.info("polling every %ss; archive=%s", interval, self.s.raw_archive_dir)
        while max_cycles is None or cycle < max_cycles:
            started = time.monotonic()
            try:
                with psycopg.connect(self.s.database_url) as conn:
                    feeds = ["trip_updates", "vehicle_positions"]
                    if cycle % ALERT_EVERY == 0:
                        feeds.append("alerts")
                    results = {f: self.fetch_one(conn, f) for f in feeds}
                log.info("cycle %d: %s", cycle, results)
            except Exception:
                log.exception("cycle %d failed", cycle)
            cycle += 1
            elapsed = time.monotonic() - started
            time.sleep(max(0.0, interval - elapsed))
