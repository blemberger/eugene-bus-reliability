select
    feed_version_id,
    trip_id,
    route_id,
    service_id,
    direction_id,
    trip_headsign,
    block_id,
    shape_id
from {{ source('gtfs', 'trips') }}
