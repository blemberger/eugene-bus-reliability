-- The "honest countdown": for a prediction the sign is showing now, where has the bus
-- actually arrived, relative to what the sign said, in the past? One row per
-- (route, time of day, timepoint or not, horizon band) with the 10th, 50th and 90th
-- percentiles of the error (observed − predicted; positive = later than the sign).
-- GROUPING SETS adds coarser rows (all times of day; all routes) so the app can fall
-- back to them when a specific combination has too few predictions.
-- Recomputed at most hourly (macros/hourly.sql).

{{ config(**hourly_config()) }}

{{ hourly_start() }}
with e as (
    select
        route_id,
        is_timepoint,
        {{ horizon_band('horizon_min') }} as horizon_band,
        {{ day_part('hour_local') }} as day_part,
        error_s
    from {{ ref('fct_prediction_errors') }}
    where horizon_min between 0 and 30
)

select
    case when grouping(route_id) = 1 then null else route_id end as route_id,
    case when grouping(day_part) = 1 then null else day_part end as day_part,
    is_timepoint,
    horizon_band,
    count(*)                                              as n_predictions,
    percentile_cont(0.1) within group (order by error_s)  as p10_error_s,
    percentile_cont(0.5) within group (order by error_s)  as p50_error_s,
    percentile_cont(0.9) within group (order by error_s)  as p90_error_s,
    count(distinct route_id)                              as n_routes
from e
group by grouping sets (
    (route_id, day_part, is_timepoint, horizon_band),
    (route_id, is_timepoint, horizon_band),
    (is_timepoint, horizon_band)
)
{{ hourly_end() }}