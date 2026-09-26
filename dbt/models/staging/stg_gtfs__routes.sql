select
    feed_version_id,
    route_id,
    route_short_name,
    route_long_name,
    route_type
from {{ source('gtfs', 'routes') }}
