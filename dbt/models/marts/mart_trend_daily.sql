-- "How has lateness changed?": the typical bus (median minutes against the timetable) and the
-- range 8 in 10 buses fell in (10th to 90th percentile) per service day, for everything, each route, each route in each direction, each stop, and each
-- route (and direction) at each stop, so the trend charts read a few rows instead of
-- computing medians over every arrival on each visit. by_* say which of route_id,
-- direction_id and stop_id the row is for (a null in one of those is then a real value, not
-- "all"). Built incrementally (macros/incremental.sql), like its source.

{{ config(
    materialized='incremental',
    incremental_strategy='delete+insert',
    unique_key='service_date',
    indexes=[
        {'columns': ['service_date']},
        {'columns': ['route_id', 'stop_id']},
        {'columns': ['stop_id']},
    ],
) }}

select
    service_date,
    grouping(route_id) = 0 as by_route,
    grouping(direction_id) = 0 as by_direction,
    grouping(stop_id) = 0 as by_stop,
    route_id, direction_id, stop_id,
    count(*) as n,
    percentile_cont(0.5) within group (order by delay_s) as median_delay_s,
    percentile_cont(0.1) within group (order by delay_s) as p10_delay_s,
    percentile_cont(0.9) within group (order by delay_s) as p90_delay_s
from {{ ref('fct_stop_events') }}
where status is not null and {{ recent_days() }}
group by service_date, grouping sets (
    (), (route_id), (route_id, direction_id), (stop_id), (route_id, stop_id),
    (route_id, direction_id, stop_id)
)