select
    feed_version_id,
    stop_id,
    stop_code,
    stop_name,
    stop_lat,
    stop_lon,
    geom
from {{ source('gtfs', 'stops') }}
where location_type = 0   -- boarding locations only; stations/entrances are not stop events
