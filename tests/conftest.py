"""Shared fixtures. Small synthetic GTFS + GTFS-RT data so tests never touch the network."""

from __future__ import annotations

import io
import os
import zipfile
from datetime import date

import pytest
from google.transit import gtfs_realtime_pb2 as gtfs_rt

STATIC_FILES = {
    "agency.txt": "agency_id,agency_name,agency_url,agency_timezone\n"
    "LTD,Lane Transit District,https://www.ltd.org,America/Los_Angeles\n",
    "routes.txt": "route_id,agency_id,route_short_name,route_long_name,route_type\n"
    "R1,LTD,1,Campbell Center,3\n",
    "stops.txt": "stop_id,stop_code,stop_name,stop_lat,stop_lon\n"
    "S1,1001,Eugene Station,44.0525,-123.0930\n"
    "S2,1002,5th & Pearl,44.0560,-123.0900\n"
    "S3,1003,8th & Oak,44.0510,-123.0910\n",
    "calendar.txt": "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
    "WK,1,1,1,1,1,0,0,20260906,20270201\n",
    "calendar_dates.txt": "service_id,date,exception_type\nWK,20261126,2\n",
    "trips.txt": "route_id,service_id,trip_id,trip_headsign,direction_id\nR1,WK,T1,Outbound,0\n",
    "stop_times.txt": "trip_id,arrival_time,departure_time,stop_id,stop_sequence,timepoint\n"
    "T1,23:50:00,23:50:00,S1,1,1\n"
    "T1,23:58:00,23:58:00,S2,2,0\n"
    "T1,24:05:00,24:05:00,S3,3,1\n",
}

SERVICE_DATE = date.today()  # keeps the maintenance-script assertion valid whenever the suite runs


@pytest.fixture
def gtfs_zip_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in STATIC_FILES.items():
            zf.writestr(name, content)
    return buf.getvalue()


def make_trip_updates(header_ts: int, arrivals: dict[int, int], delay: int = 0) -> bytes:
    """Build a TripUpdates feed for trip T1 with {stop_sequence: arrival epoch}."""
    msg = gtfs_rt.FeedMessage()
    msg.header.gtfs_realtime_version = "2.0"
    msg.header.timestamp = header_ts
    ent = msg.entity.add()
    ent.id = "tu-T1"
    tu = ent.trip_update
    tu.trip.trip_id = "T1"
    tu.trip.route_id = "R1"
    tu.trip.start_date = SERVICE_DATE.strftime("%Y%m%d")
    tu.vehicle.id = "bus-42"
    tu.timestamp = header_ts
    for seq, arr in sorted(arrivals.items()):
        stu = tu.stop_time_update.add()
        stu.stop_sequence = seq
        stu.stop_id = f"S{seq}"
        stu.arrival.time = arr
        stu.arrival.delay = delay
        stu.arrival.scheduled_time = arr - delay
    return msg.SerializeToString()


def make_vehicle_positions(header_ts: int, lat: float, lon: float, pos_ts: int) -> bytes:
    msg = gtfs_rt.FeedMessage()
    msg.header.gtfs_realtime_version = "2.0"
    msg.header.timestamp = header_ts
    ent = msg.entity.add()
    ent.id = "vp-42"
    v = ent.vehicle
    v.vehicle.id = "bus-42"
    v.trip.trip_id = "T1"
    v.trip.route_id = "R1"
    v.trip.start_date = SERVICE_DATE.strftime("%Y%m%d")
    v.position.latitude = lat
    v.position.longitude = lon
    v.position.bearing = 90.0
    v.timestamp = pos_ts
    v.current_stop_sequence = 2
    v.current_status = 2
    v.occupancy_status = 1
    v.occupancy_percentage = 20
    return msg.SerializeToString()


def make_alerts(header_ts: int) -> bytes:
    msg = gtfs_rt.FeedMessage()
    msg.header.gtfs_realtime_version = "2.0"
    msg.header.timestamp = header_ts
    ent = msg.entity.add()
    ent.id = "alert-1"
    ent.alert.header_text.translation.add().text = "Detour on Route 1"
    return msg.SerializeToString()


@pytest.fixture
def database_url() -> str:
    """Integration tests need their own database: they TRUNCATE everything.

    Points at TEST_DATABASE_URL (compose service db_test, port 5433), never at
    DATABASE_URL. Skipped when unset, so `make test` works without Docker.
    """
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set; integration tests need the db_test service")
    if url == os.environ.get("DATABASE_URL"):
        pytest.fail("TEST_DATABASE_URL must not equal DATABASE_URL: the tests wipe every table")
    return url
