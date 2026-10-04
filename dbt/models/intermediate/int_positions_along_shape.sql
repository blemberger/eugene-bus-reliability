-- Each vehicle position placed along its trip's route shape: how far along the route (0..1)
-- and how far off it (metres). The positions of a trip are walked in time order along the
-- shape (macros/shape_walk.sql), each placed on the first pass of the shape ahead of where the
-- bus had got to, within the distance it could have travelled since its previous report. On
-- routes that use the same street twice (out and back, or a loop), the nearest point on the
-- whole shape could be the other pass, which would mark stops in between as passed too early.
-- Built incrementally (macros/incremental.sql): each build recomputes the latest two service days.

{{ config(
    materialized='incremental',
    incremental_strategy='delete+insert',
    unique_key='service_date',
    indexes=[
        {'columns': ['trip_id', 'service_date', 'ts']},
        {'columns': ['service_date']},
    ]
) }}

with trip_shape as (
    select fv.service_date, t.feed_version_id, t.trip_id, l.line_m
    from {{ ref('int_feed_version_by_date') }} fv
    join {{ ref('stg_gtfs__trips') }} t on t.feed_version_id = fv.feed_version_id
    join {{ ref('int_shape_lines') }} l on l.shape_id = t.shape_id and l.feed_version_id = t.feed_version_id
    where {{ recent_days('fv.service_date') }} and fv.service_date <= current_date
),

per_trip as (
    select
        trip_id, service_date,
        array_agg(vehicle_id order by position_timestamp, vehicle_id) as vehicle_ids,
        array_agg(position_timestamp order by position_timestamp, vehicle_id) as tss,
        array_agg(ST_Transform(geom::geometry, 32610) order by position_timestamp, vehicle_id) as pts
    from {{ ref('stg_rt__vehicle_positions') }}
    where trip_id is not null and service_date is not null
      and {{ recent_days() }}
    group by 1, 2
)

select
    p.trip_id,
    p.service_date,
    p.vehicle_ids[w.idx] as vehicle_id,
    p.tss[w.idx] as ts,
    w.frac,
    w.dist_m as off_route_m
from per_trip p
join trip_shape s on s.trip_id = p.trip_id and s.service_date = p.service_date
cross join lateral analytics.shape_walk(s.line_m, p.pts, p.tss) w