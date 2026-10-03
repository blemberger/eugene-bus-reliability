"""Load a static GTFS zip into the gtfs.* tables, versioned by content hash."""

from __future__ import annotations

import csv
import hashlib
import io
import logging
import zipfile
from collections.abc import Callable, Iterator
from datetime import date, datetime

import httpx
import psycopg

from eugene_bus_reliability import USER_AGENT
from eugene_bus_reliability.db import copy_rows

log = logging.getLogger(__name__)


def gtfs_time_to_seconds(value: str | None) -> int | None:
    """'25:10:00' -> 90600. GTFS times may exceed 24h; hours may be one or two digits."""
    if value is None or value == "":
        return None
    parts = str(value).strip().split(":")
    if len(parts) != 3:
        raise ValueError(f"malformed GTFS time {value!r}")
    h, m, s = (int(p) for p in parts)
    if not (0 <= m < 60 and 0 <= s < 60):
        raise ValueError(f"malformed GTFS time {value!r}")
    return h * 3600 + m * 60 + s


def gtfs_date(value: str | None) -> date | None:
    """'20260906' -> date(2026, 9, 6)."""
    if value is None or value == "":
        return None
    return datetime.strptime(str(value).strip(), "%Y%m%d").date()


def _s(v: str | None) -> str | None:
    return None if v is None or v == "" else v


def _i(v: str | None) -> int | None:
    return None if v is None or v == "" else int(v)


def _f(v: str | None) -> float | None:
    return None if v is None or v == "" else float(v)


def _b(v: str | None) -> bool:
    return v == "1"


TABLES: list[tuple[str, str, tuple[str, ...], Callable[[dict[str, str]], tuple]]] = [
    (
        "agency.txt",
        "gtfs.agency",
        ("agency_id", "agency_name", "agency_url", "agency_timezone"),
        lambda r: (
            r.get("agency_id", ""),
            r["agency_name"],
            _s(r.get("agency_url")),
            r["agency_timezone"],
        ),
    ),
    (
        "routes.txt",
        "gtfs.routes",
        (
            "route_id",
            "agency_id",
            "route_short_name",
            "route_long_name",
            "route_type",
            "route_color",
        ),
        lambda r: (
            r["route_id"],
            r.get("agency_id", ""),
            _s(r.get("route_short_name")),
            _s(r.get("route_long_name")),
            _i(r["route_type"]),
            _s(r.get("route_color")),
        ),
    ),
    (
        "stops.txt",
        "gtfs.stops",
        (
            "stop_id",
            "stop_code",
            "stop_name",
            "stop_lat",
            "stop_lon",
            "location_type",
            "parent_station",
        ),
        lambda r: (
            r["stop_id"],
            _s(r.get("stop_code")),
            r["stop_name"],
            _f(r["stop_lat"]),
            _f(r["stop_lon"]),
            _i(r.get("location_type")) or 0,
            _s(r.get("parent_station")),
        ),
    ),
    (
        "calendar.txt",
        "gtfs.calendar",
        (
            "service_id",
            "monday",
            "tuesday",
            "wednesday",
            "thursday",
            "friday",
            "saturday",
            "sunday",
            "start_date",
            "end_date",
        ),
        lambda r: (
            r["service_id"],
            _b(r["monday"]),
            _b(r["tuesday"]),
            _b(r["wednesday"]),
            _b(r["thursday"]),
            _b(r["friday"]),
            _b(r["saturday"]),
            _b(r["sunday"]),
            gtfs_date(r["start_date"]),
            gtfs_date(r["end_date"]),
        ),
    ),
    (
        "calendar_dates.txt",
        "gtfs.calendar_dates",
        ("service_id", "date", "exception_type"),
        lambda r: (r["service_id"], gtfs_date(r["date"]), _i(r["exception_type"])),
    ),
    (
        "trips.txt",
        "gtfs.trips",
        (
            "trip_id",
            "route_id",
            "service_id",
            "trip_headsign",
            "direction_id",
            "block_id",
            "shape_id",
        ),
        lambda r: (
            r["trip_id"],
            r["route_id"],
            r["service_id"],
            _s(r.get("trip_headsign")),
            _i(r.get("direction_id")),
            _s(r.get("block_id")),
            _s(r.get("shape_id")),
        ),
    ),
    (
        "stop_times.txt",
        "gtfs.stop_times",
        (
            "trip_id",
            "stop_sequence",
            "stop_id",
            "arrival_seconds",
            "departure_seconds",
            "timepoint",
            "pickup_type",
            "drop_off_type",
            "shape_dist_traveled",
        ),
        lambda r: (
            r["trip_id"],
            _i(r["stop_sequence"]),
            r["stop_id"],
            gtfs_time_to_seconds(r.get("arrival_time")),
            gtfs_time_to_seconds(r.get("departure_time")),
            _i(r.get("timepoint")),
            _i(r.get("pickup_type")),
            _i(r.get("drop_off_type")),
            _f(r.get("shape_dist_traveled")),
        ),
    ),
    (
        "shapes.txt",
        "gtfs.shapes",
        ("shape_id", "shape_pt_sequence", "shape_pt_lat", "shape_pt_lon", "shape_dist_traveled"),
        lambda r: (
            r["shape_id"],
            _i(r["shape_pt_sequence"]),
            _f(r["shape_pt_lat"]),
            _f(r["shape_pt_lon"]),
            _f(r.get("shape_dist_traveled")),
        ),
    ),
]
REQUIRED = {"agency.txt", "routes.txt", "stops.txt", "trips.txt", "stop_times.txt"}


def read_csv(zf: zipfile.ZipFile, name: str) -> Iterator[dict[str, str]]:
    """Yield one dict per row. utf-8-sig strips the BOM some agencies put in front."""
    with zf.open(name) as raw:
        text = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
        for row in csv.DictReader(text):
            yield {k.strip(): (v or "").strip() for k, v in row.items() if k is not None}


def rows_for(zf: zipfile.ZipFile, filename: str, feed_version_id: int) -> Iterator[tuple]:
    """Typed rows for one GTFS file, each prefixed with feed_version_id."""
    _, _, _, build = next(t for t in TABLES if t[0] == filename)
    for r in read_csv(zf, filename):
        yield (feed_version_id, *build(r))


def feed_info(zf: zipfile.ZipFile) -> tuple[date | None, date | None, str | None]:
    if "feed_info.txt" not in zf.namelist():
        return None, None, None
    first = next(read_csv(zf, "feed_info.txt"), {})
    return (
        gtfs_date(first.get("feed_start_date")),
        gtfs_date(first.get("feed_end_date")),
        _s(first.get("feed_version")),
    )


def download(url: str) -> bytes:
    with httpx.Client(
        timeout=60, follow_redirects=True, headers={"User-Agent": USER_AGENT}
    ) as client:
        r = client.get(url)
        r.raise_for_status()
        return r.content


def load_zip(conn: psycopg.Connection, data: bytes, source_url: str) -> int | None:
    """Load a GTFS zip. Returns the new feed_version_id, or None if this exact
    zip (by sha256) was already loaded."""
    digest = hashlib.sha256(data).digest()
    with conn.cursor() as cur:
        cur.execute("SELECT feed_version_id FROM gtfs.feed_version WHERE sha256 = %s", (digest,))
        row = cur.fetchone()
        if row:
            log.info("GTFS zip already loaded as feed_version_id=%s", row[0])
            return None

    zf = zipfile.ZipFile(io.BytesIO(data))
    names = set(zf.namelist())
    missing = REQUIRED - names
    if missing:
        raise FileNotFoundError(f"GTFS zip is missing {sorted(missing)}")
    if "calendar.txt" not in names and "calendar_dates.txt" not in names:
        raise FileNotFoundError("GTFS zip has neither calendar.txt nor calendar_dates.txt")
    start, end, version = feed_info(zf)

    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO gtfs.feed_version
                    (sha256, source_url, feed_start_date, feed_end_date, feed_version, byte_size)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING feed_version_id
                """,
                (digest, source_url, start, end, version, len(data)),
            )
            fv = cur.fetchone()[0]
        for filename, table, columns, _ in TABLES:
            if filename not in names:
                continue
            n = copy_rows(conn, table, ("feed_version_id", *columns), rows_for(zf, filename, fv))
            log.info("%s: %d rows", table, n)

    log.info("loaded feed_version_id=%s (%s..%s)", fv, start, end)
    return fv


def load_from_url(conn: psycopg.Connection, url: str) -> int | None:
    log.info("downloading %s", url)
    return load_zip(conn, download(url), url)
