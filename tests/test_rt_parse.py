from datetime import UTC, date, datetime

from conftest import SERVICE_DATE, make_trip_updates, make_vehicle_positions

from eugene_bus_reliability import rt_parse
from eugene_bus_reliability.poller import service_date_for


def test_trip_updates_parse():
    msg = rt_parse.parse_feed(
        make_trip_updates(1_800_000_000, {1: 1_800_000_100, 2: 1_800_000_500})
    )
    trips, preds = rt_parse.trip_updates(msg, fallback_start_date=date(2000, 1, 1))
    assert len(trips) == 1
    assert trips[0].trip_id == "T1"
    assert trips[0].start_date == SERVICE_DATE  # from the feed, not the fallback
    assert trips[0].vehicle_id == "bus-42"
    assert [p.stop_sequence for p in preds] == [1, 2]
    assert preds[1].arrival_time == datetime.fromtimestamp(1_800_000_500, tz=UTC)
    assert preds[1].departure_time is None
    assert preds[1].arrival_delay == 0
    assert preds[1].scheduled_time == datetime.fromtimestamp(1_800_000_500, tz=UTC)


def test_stop_time_update_without_sequence_is_dropped():
    from google.transit import gtfs_realtime_pb2 as gtfs_rt

    msg = gtfs_rt.FeedMessage()
    msg.header.timestamp = 1
    tu = msg.entity.add().trip_update
    tu.trip.trip_id = "T1"
    stu = tu.stop_time_update.add()
    stu.stop_id = "S9"  # no stop_sequence
    _, preds = rt_parse.trip_updates(msg, fallback_start_date=date(2026, 1, 1))
    assert preds == []


def test_vehicle_positions_parse():
    msg = rt_parse.parse_feed(make_vehicle_positions(1_800_000_000, 44.05, -123.09, 1_799_999_990))
    rows = rt_parse.vehicle_positions(msg, fallback_ts=datetime.now(tz=UTC))
    assert len(rows) == 1
    r = rows[0]
    assert r.vehicle_id == "bus-42"
    assert r.position_timestamp == datetime.fromtimestamp(1_799_999_990, tz=UTC)
    assert r.current_stop_sequence == 2 and r.current_status == 2
    assert r.bearing == 90.0 and r.speed_mps is None
    assert r.occupancy_status == 1 and r.occupancy_percentage == 20


def test_service_date_rolls_at_3am_local():
    # 02:30 PDT on Sep 16 = 09:30 UTC -> still service day Sep 15
    assert service_date_for(datetime(2026, 9, 16, 9, 30, tzinfo=UTC)) == date(2026, 9, 15)
    # 03:30 PDT on Sep 16 = 10:30 UTC -> service day Sep 16
    assert service_date_for(datetime(2026, 9, 16, 10, 30, tzinfo=UTC)) == date(2026, 9, 16)
