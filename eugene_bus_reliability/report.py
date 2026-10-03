"""Collection report: what has the poller collected? `python -m eugene_bus_reliability report`.

Totals per feed are all-time; everything that scans the large realtime tables is
limited to recent days so the report stays fast as history grows."""

from __future__ import annotations

import psycopg

QUERIES: list[tuple[str, str]] = [
    (
        "Schedule versions loaded",
        """
        SELECT feed_version_id, downloaded_at::date AS downloaded,
               feed_start_date, feed_end_date,
               (SELECT count(*) FROM gtfs.trips t WHERE t.feed_version_id = v.feed_version_id) AS trips,
               (SELECT count(*) FROM gtfs.stop_times s WHERE s.feed_version_id = v.feed_version_id) AS stop_times
        FROM gtfs.feed_version v ORDER BY feed_version_id""",
    ),
    (
        "Stored fetches per feed",
        """
        SELECT feed, count(*) AS fetches, min(fetched_at) AS first, max(fetched_at) AS last,
               round(avg(byte_size)) AS avg_bytes
        FROM rt.fetch GROUP BY feed ORDER BY feed""",
    ),
    (
        "Feed update cadence (seconds between consecutive header timestamps, last 2 hours)",
        """
        SELECT feed,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY gap) AS median_s,
               min(gap) AS min_s, max(gap) AS max_s
        FROM (
            SELECT feed,
                   extract(epoch FROM header_timestamp
                           - lag(header_timestamp) OVER (PARTITION BY feed ORDER BY header_timestamp)) AS gap
            FROM rt.fetch WHERE fetched_at > now() - interval '2 hours'
        ) g WHERE gap IS NOT NULL GROUP BY feed ORDER BY feed""",
    ),
    (
        "Polls discarded because the header had not advanced (by hour)",
        """
        SELECT feed, hour, unchanged_count FROM rt.fetch_unchanged
        ORDER BY hour DESC, feed LIMIT 12""",
    ),
    (
        "Vehicle positions, last 7 days",
        """
        SELECT count(*) AS rows, count(DISTINCT vehicle_id) AS vehicles,
               count(DISTINCT trip_id) AS trips, min(position_timestamp) AS first, max(position_timestamp) AS last
        FROM rt.vehicle_position WHERE position_timestamp > now() - interval '7 days'""",
    ),
    (
        "Predictions: current rows, and rows superseded in the last 7 days",
        """
        SELECT (SELECT count(*) FROM rt.prediction_current) AS current_rows,
               (SELECT count(*) FROM rt.prediction_history
                 WHERE closed_at > now() - interval '7 days') AS superseded_rows_7d""",
    ),
    (
        "How often does a prediction change once made? (per trip-stop, last 7 service days)",
        """
        SELECT count(*) FILTER (WHERE n = 1) AS never_changed,
               count(*) FILTER (WHERE n BETWEEN 2 AND 5) AS changed_1_to_4_times,
               count(*) FILTER (WHERE n > 5) AS changed_5_plus
        FROM (
            SELECT trip_id, start_date, stop_sequence, count(*) AS n
            FROM (SELECT trip_id, start_date, stop_sequence FROM rt.prediction_current
                  WHERE start_date >= current_date - 7
                  UNION ALL
                  SELECT trip_id, start_date, stop_sequence FROM rt.prediction_history
                  WHERE start_date >= current_date - 7) u
            GROUP BY 1, 2, 3
        ) k""",
    ),
    (
        "Realtime trip_ids that do not match the loaded schedule (should be ~0)",
        """
        SELECT count(DISTINCT p.trip_id) AS unmatched_trip_ids
        FROM rt.prediction_current p
        WHERE NOT EXISTS (SELECT 1 FROM gtfs.trips t WHERE t.trip_id = p.trip_id)""",
    ),
]


def run(database_url: str) -> None:
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        for title, q in QUERIES:
            cur.execute(q)
            cols = [d.name for d in cur.description]
            rows = cur.fetchall()
            print(f"\n== {title}")
            print(" | ".join(cols))
            for r in rows:
                print(" | ".join("" if v is None else str(v) for v in r))
