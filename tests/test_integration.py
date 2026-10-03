"""Integration tests. Skipped unless TEST_DATABASE_URL points at a database that has
had sql/schema/*.sql applied (`make up` starts one: the db_test service)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
import psycopg
import pytest
from conftest import make_alerts, make_trip_updates, make_vehicle_positions

from eugene_bus_reliability import gtfs_static, replay, rt_parse
from eugene_bus_reliability.config import Settings
from eugene_bus_reliability.poller import Poller

pytestmark = pytest.mark.integration

MAINTENANCE_SQL = (
    Path(__file__).resolve().parents[1] / "sql/maintenance/close_out_stale_predictions.sql"
)

TRUNCATE_ALL = """
    TRUNCATE rt.alert, rt.prediction_history, rt.prediction_current, rt.trip_update,
             rt.vehicle_position, rt.fetch_unchanged, rt.fetch,
             gtfs.shapes, gtfs.stop_times, gtfs.trips, gtfs.calendar_dates,
             gtfs.calendar, gtfs.stops, gtfs.routes, gtfs.agency, gtfs.feed_version
    RESTART IDENTITY CASCADE
"""

T = 1_800_000_000  # header timestamp of the first poll in the scenarios below


def _ts(epoch: int) -> datetime:
    return datetime.fromtimestamp(epoch, tz=UTC)


@pytest.fixture
def clean_db(database_url):
    with psycopg.connect(database_url) as conn:
        conn.execute(TRUNCATE_ALL)
    yield database_url
    with psycopg.connect(database_url) as conn:
        conn.execute(TRUNCATE_ALL)


def _settings(database_url, tmp_path) -> Settings:
    return Settings(
        database_url=database_url,
        gtfs_static_url="unused",
        trip_updates_url="https://x/tu",
        vehicle_positions_url="https://x/vp",
        alerts_url="https://x/al",
        poll_interval_seconds=0,
        raw_archive_dir=tmp_path,
    )


def _poller(database_url, tmp_path, responses: dict[str, list[bytes]]) -> Poller:
    """Poller whose HTTP client returns canned feed bytes, one per call, per feed."""
    queues = {k: list(v) for k, v in responses.items()}

    def handler(request: httpx.Request) -> httpx.Response:
        key = request.url.path.strip("/")
        q = queues[key]
        body = q.pop(0) if len(q) > 1 else q[0]  # last response repeats
        return httpx.Response(200, content=body)

    p = Poller(_settings(database_url, tmp_path))
    p.client = httpx.Client(transport=httpx.MockTransport(handler))
    return p


def _four_polls() -> dict[str, list[bytes]]:
    """Four poll cycles: one repeated header (discarded), one new header with unchanged
    values (intervals extend), one with changed values (old rows closed into history)."""
    t = T
    return {
        "tu": [
            make_trip_updates(t, {1: t + 100, 2: t + 500, 3: t + 900}),  # fetch 1
            make_trip_updates(t, {1: t + 100, 2: t + 500, 3: t + 900}),  # same header: discarded
            make_trip_updates(t + 30, {1: t + 100, 2: t + 500, 3: t + 900}),  # same values
            make_trip_updates(t + 60, {1: t + 100, 2: t + 560, 3: t + 960}),  # stops 2, 3 change
        ],
        "vp": [
            make_vehicle_positions(t, 44.05, -123.09, t - 5),
            make_vehicle_positions(t + 30, 44.051, -123.091, t + 25),
            make_vehicle_positions(t + 30, 44.051, -123.091, t + 25),  # same header
            make_vehicle_positions(t + 60, 44.052, -123.092, t + 55),
        ],
        "al": [make_alerts(t)],
    }


def _snapshot(conn) -> dict[str, list[tuple]]:
    queries = {
        "fetch": """SELECT feed, header_timestamp, entity_count, byte_size, sha256, archive_path
                    FROM rt.fetch ORDER BY 1, 2""",
        "current": """SELECT trip_id, start_date, stop_sequence, stop_id, arrival_time,
                             departure_time, scheduled_time, first_seen_at, last_seen_at
                      FROM rt.prediction_current ORDER BY 1, 2, 3""",
        "history": """SELECT trip_id, start_date, stop_sequence, arrival_time, first_seen_at,
                             last_seen_at, closed_at
                      FROM rt.prediction_history ORDER BY 1, 2, 3, 5""",
        "positions": """SELECT vehicle_id, position_timestamp, latitude, longitude, trip_id
                        FROM rt.vehicle_position ORDER BY 1, 2""",
    }
    return {k: conn.execute(q).fetchall() for k, q in queries.items()}


def test_static_load_is_idempotent(clean_db, gtfs_zip_bytes):
    with psycopg.connect(clean_db) as conn:
        fv = gtfs_static.load_zip(conn, gtfs_zip_bytes, "test://fixture")
        assert fv == 1
        assert gtfs_static.load_zip(conn, gtfs_zip_bytes, "test://fixture") is None
        n_stop_times, late = conn.execute(
            "SELECT count(*), max(arrival_seconds) FROM gtfs.stop_times"
        ).fetchone()
        assert n_stop_times == 3
        assert late == 24 * 3600 + 5 * 60
        # PostGIS generated column populated
        (dist,) = conn.execute("""
            SELECT ST_Distance(a.geom, b.geom) FROM gtfs.stops a, gtfs.stops b
            WHERE a.stop_id = 'S1' AND b.stop_id = 'S2'
        """).fetchone()
        assert 300 < dist < 600  # metres


def test_change_only_predictions(clean_db, gtfs_zip_bytes, tmp_path):
    with psycopg.connect(clean_db) as conn:
        gtfs_static.load_zip(conn, gtfs_zip_bytes, "test://fixture")

    t = T
    _poller(clean_db, tmp_path, _four_polls()).run(max_cycles=4)

    with psycopg.connect(clean_db) as conn:
        fetches = dict(conn.execute("SELECT feed, count(*) FROM rt.fetch GROUP BY feed").fetchall())
        assert fetches == {"trip_updates": 3, "vehicle_positions": 3, "alerts": 1}

        (unchanged,) = conn.execute(
            "SELECT sum(unchanged_count) FROM rt.fetch_unchanged"
        ).fetchone()
        assert unchanged == 2

        # Stop 1 never changed: one current row, first_seen = t, last_seen = t+60
        row = conn.execute("""
            SELECT extract(epoch FROM first_seen_at), extract(epoch FROM last_seen_at)
            FROM rt.prediction_current WHERE trip_id = 'T1' AND stop_sequence = 1
        """).fetchone()
        assert row == (t, t + 60)
        assert (
            conn.execute(
                "SELECT count(*) FROM rt.prediction_history WHERE stop_sequence = 1"
            ).fetchone()[0]
            == 0
        )

        # Stop 2 changed at t+60: old value closed into history with the interval it was
        # held, and closed_at is the header time of the message that superseded it
        hist = conn.execute("""
            SELECT extract(epoch FROM arrival_time), extract(epoch FROM first_seen_at),
                   extract(epoch FROM last_seen_at), extract(epoch FROM closed_at)
            FROM rt.prediction_history WHERE stop_sequence = 2
        """).fetchall()
        assert hist == [(t + 500, t, t + 30, t + 60)]
        cur = conn.execute("""
            SELECT extract(epoch FROM arrival_time), extract(epoch FROM first_seen_at)
            FROM rt.prediction_current WHERE stop_sequence = 2
        """).fetchone()
        assert cur == (t + 560, t + 60)

        # Vehicle positions: 3 distinct position timestamps
        assert conn.execute("SELECT count(*) FROM rt.vehicle_position").fetchone()[0] == 3

        # Raw archive written once per stored fetch
        assert sum(1 for _ in tmp_path.rglob("*.pb.gz")) == 7

        # Maintenance script runs and leaves the current table for today's service day alone
        conn.execute(MAINTENANCE_SQL.read_text())
        assert conn.execute("SELECT count(*) FROM rt.prediction_current").fetchone()[0] == 3


def test_older_message_does_not_overwrite_newer(clean_db, gtfs_zip_bytes, tmp_path):
    with psycopg.connect(clean_db) as conn:
        gtfs_static.load_zip(conn, gtfs_zip_bytes, "test://fixture")
    poller = _poller(clean_db, tmp_path, _four_polls())
    poller.run(max_cycles=4)

    with psycopg.connect(clean_db) as conn:
        before = _snapshot(conn)
        # header t+45 falls between stored messages; its values differ from everything stored
        t = T
        data = make_trip_updates(t + 45, {1: t + 111, 2: t + 555, 3: t + 999})
        stored = poller.store(
            conn, "trip_updates", data, rt_parse.parse_feed(data), _ts(t + 45), _ts(t + 45), None
        )
        assert stored  # the message itself is recorded in rt.fetch ...
        after = _snapshot(conn)
    assert after["current"] == before["current"]  # ... but no prediction moved
    assert after["history"] == before["history"]


def test_rebuild_from_archive_reproduces_tables(clean_db, gtfs_zip_bytes, tmp_path):
    with psycopg.connect(clean_db) as conn:
        gtfs_static.load_zip(conn, gtfs_zip_bytes, "test://fixture")
    _poller(clean_db, tmp_path, _four_polls()).run(max_cycles=4)

    with psycopg.connect(clean_db) as conn:
        live = _snapshot(conn)
    assert live["history"]  # the scenario includes superseded predictions

    replay.main(_settings(clean_db, tmp_path), rebuild=True)

    with psycopg.connect(clean_db) as conn:
        rebuilt = _snapshot(conn)
    assert rebuilt == live


def test_daily_cleanup_moves_old_days_intact(clean_db, gtfs_zip_bytes, tmp_path):
    with psycopg.connect(clean_db) as conn:
        gtfs_static.load_zip(conn, gtfs_zip_bytes, "test://fixture")
    t = T
    one_poll = {
        "tu": [make_trip_updates(t, {1: t + 100, 2: t + 500, 3: t + 900})],
        "vp": [make_vehicle_positions(t, 44.05, -123.09, t - 5)],
        "al": [make_alerts(t)],
    }
    _poller(clean_db, tmp_path, one_poll).run(max_cycles=1)

    with psycopg.connect(clean_db) as conn:
        conn.execute("UPDATE rt.prediction_current SET start_date = start_date - 5")
        conn.execute(MAINTENANCE_SQL.read_text())
        assert conn.execute("SELECT count(*) FROM rt.prediction_current").fetchone()[0] == 0
        moved = conn.execute("""
            SELECT stop_sequence, extract(epoch FROM arrival_time), extract(epoch FROM scheduled_time),
                   closed_at IS NOT NULL
            FROM rt.prediction_history ORDER BY stop_sequence
        """).fetchall()
    # every column landed where it belongs: the fixture's scheduled_time equals arrival_time
    assert moved == [
        (1, t + 100, t + 100, True),
        (2, t + 500, t + 500, True),
        (3, t + 900, t + 900, True),
    ]
