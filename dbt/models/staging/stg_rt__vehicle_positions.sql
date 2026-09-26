select
    fetch_id,
    vehicle_id,
    position_timestamp,
    trip_id,
    route_id,
    start_date as service_date,
    latitude,
    longitude,
    bearing,
    speed_mps,
    stop_id,
    current_stop_sequence,
    current_status,
    occupancy_status,
    occupancy_percentage,
    geom
from {{ source('rt', 'vehicle_position') }}