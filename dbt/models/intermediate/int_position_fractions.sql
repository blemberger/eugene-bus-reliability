-- Each vehicle position projected onto its trip's route shape: how far along the
-- route (0..1) and how far off it (metres). Kept as its own table so it can be
-- inspected, indexed, and later made incremental; it is the expensive step.

{{ config(
    indexes=[
        {'columns': ['trip_id', 'service_date', 'ts']},
        {'columns': ['service_date']},
    ]
) }}

with trip_shape as (
    select fv.service_date, t.feed_version_id, t.trip_id, l.line
    from {{ ref('int_feed_version_by_date') }} fv
    join {{ ref('stg_gtfs__trips') }} t on t.feed_version_id = fv.feed_version_id
    join {{ ref('int_shape_lines') }} l on l.shape_id = t.shape_id and l.feed_version_id = t.feed_version_id
    where fv.service_date between current_date - {{ var('lookback_days') }} and current_date
)

select
    vp.trip_id,
    vp.service_date,
    vp.vehicle_id,
    vp.position_timestamp as ts,
    ST_LineLocatePoint(s.line, vp.geom::geometry) as frac,
    ST_Distance(s.line::geography, vp.geom) as off_route_m
from {{ ref('stg_rt__vehicle_positions') }} vp
join trip_shape s on s.trip_id = vp.trip_id and s.service_date = vp.service_date
where vp.trip_id is not null and vp.service_date is not null