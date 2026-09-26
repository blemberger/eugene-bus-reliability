"""Pure functions: GTFS-Realtime FeedMessage -> plain row tuples.

No I/O here, so everything is unit-testable against fixture feeds.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime

from google.protobuf.json_format import MessageToDict
from google.transit import gtfs_realtime_pb2 as gtfs_rt


def _ts(seconds: int) -> datetime | None:
    return datetime.fromtimestamp(seconds, tz=UTC) if seconds else None


def _start_date(value: str) -> date | None:
    return datetime.strptime(value, "%Y%m%d").date() if value else None


def parse_feed(data: bytes) -> gtfs_rt.FeedMessage:
    msg = gtfs_rt.FeedMessage()
    msg.ParseFromString(data)
    return msg


@dataclass(frozen=True)
class VehiclePositionRow:
    vehicle_id: str
    position_timestamp: datetime
    trip_id: str | None
    route_id: str | None
    start_date: date | None
    latitude: float
    longitude: float
    bearing: float | None
    speed_mps: float | None
    stop_id: str | None
    current_stop_sequence: int | None
    current_status: int | None
    occupancy_status: int | None
    occupancy_percentage: int | None


@dataclass(frozen=True)
class TripUpdateRow:
    trip_id: str
    start_date: date | None
    route_id: str | None
    vehicle_id: str | None
    schedule_relationship: int | None
    trip_timestamp: datetime | None
    trip_delay_seconds: int | None


@dataclass(frozen=True)
class PredictionRow:
    trip_id: str
    start_date: date
    stop_sequence: int
    stop_id: str | None
    arrival_time: datetime | None
    departure_time: datetime | None
    arrival_delay: int | None
    departure_delay: int | None
    schedule_relationship: int | None
    scheduled_time: datetime | None


def vehicle_positions(msg: gtfs_rt.FeedMessage, fallback_ts: datetime) -> list[VehiclePositionRow]:
    rows = []
    for ent in msg.entity:
        if not ent.HasField("vehicle"):
            continue
        v = ent.vehicle
        if not v.HasField("position"):
            continue
        vid = v.vehicle.id or v.vehicle.label or ent.id
        rows.append(
            VehiclePositionRow(
                vehicle_id=vid,
                position_timestamp=_ts(v.timestamp) or fallback_ts,
                trip_id=v.trip.trip_id or None,
                route_id=v.trip.route_id or None,
                start_date=_start_date(v.trip.start_date),
                latitude=v.position.latitude,
                longitude=v.position.longitude,
                bearing=v.position.bearing if v.position.HasField("bearing") else None,
                speed_mps=v.position.speed if v.position.HasField("speed") else None,
                stop_id=v.stop_id or None,
                current_stop_sequence=(
                    v.current_stop_sequence if v.HasField("current_stop_sequence") else None
                ),
                current_status=v.current_status if v.HasField("current_status") else None,
                occupancy_status=v.occupancy_status if v.HasField("occupancy_status") else None,
                occupancy_percentage=(
                    v.occupancy_percentage if v.HasField("occupancy_percentage") else None
                ),
            )
        )
    return rows


def trip_updates(
    msg: gtfs_rt.FeedMessage, fallback_start_date: date
) -> tuple[list[TripUpdateRow], list[PredictionRow]]:
    """Returns trip-level rows and one prediction row per StopTimeUpdate.

    A StopTimeUpdate without stop_sequence is dropped: without it the prediction
    cannot be matched to a scheduled stop event. LTD's feed sets it; the count of
    dropped updates is worth logging if that ever changes.
    """
    trips, preds = [], []
    for ent in msg.entity:
        if not ent.HasField("trip_update"):
            continue
        tu = ent.trip_update
        start = _start_date(tu.trip.start_date) or fallback_start_date
        trips.append(
            TripUpdateRow(
                trip_id=tu.trip.trip_id,
                start_date=start,
                route_id=tu.trip.route_id or None,
                vehicle_id=tu.vehicle.id or tu.vehicle.label or None,
                schedule_relationship=(
                    tu.trip.schedule_relationship
                    if tu.trip.HasField("schedule_relationship")
                    else None
                ),
                trip_timestamp=_ts(tu.timestamp),
                trip_delay_seconds=tu.delay if tu.HasField("delay") else None,
            )
        )
        for stu in tu.stop_time_update:
            if not stu.HasField("stop_sequence"):
                continue
            arr, dep = stu.arrival, stu.departure
            # The feed's own scheduled time, from whichever event it sent (departure at
            # most stops for LTD). GTFS-RT added this field in 2024; older feeds omit it.
            scheduled = None
            if stu.HasField("departure") and dep.HasField("scheduled_time"):
                scheduled = _ts(dep.scheduled_time)
            elif stu.HasField("arrival") and arr.HasField("scheduled_time"):
                scheduled = _ts(arr.scheduled_time)
            preds.append(
                PredictionRow(
                    trip_id=tu.trip.trip_id,
                    start_date=start,
                    stop_sequence=stu.stop_sequence,
                    stop_id=stu.stop_id or None,
                    arrival_time=_ts(arr.time) if stu.HasField("arrival") else None,
                    departure_time=_ts(dep.time) if stu.HasField("departure") else None,
                    arrival_delay=(
                        arr.delay if stu.HasField("arrival") and arr.HasField("delay") else None
                    ),
                    departure_delay=(
                        dep.delay if stu.HasField("departure") and dep.HasField("delay") else None
                    ),
                    schedule_relationship=(
                        stu.schedule_relationship if stu.HasField("schedule_relationship") else None
                    ),
                    scheduled_time=scheduled,
                )
            )
    return trips, preds


def alerts(msg: gtfs_rt.FeedMessage) -> list[tuple[str, dict]]:
    return [
        (ent.id, MessageToDict(ent.alert, preserving_proto_field_name=True))
        for ent in msg.entity
        if ent.HasField("alert")
    ]
