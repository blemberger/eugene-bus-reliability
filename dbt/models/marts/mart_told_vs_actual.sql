-- "How much later than you were told did the bus come?", for each thing that tells you:
--   basis 'timetable'  the printed schedule (ahead_min null): actual − scheduled arrival, one
--                      row per scored stop event (the same lateness as the rest of the site);
--   basis 'sign'       LTD's live prediction when it said the bus was ahead_min minutes away
--                      (1, 2, 3, 5, 10, 15, 20, 30): actual − predicted arrival, one row per
--                      prediction shown (fct_prediction_errors; horizon_min is whole minutes, so
--                      "5" means the sign showed 5:00 to 5:59).
-- Positive = the bus came later than you were told. Rows for all routes and stops together
-- (route_id and stop_id null), per route, and per stop. All data. The Accuracy page and each
-- stop's page draw it as one chart, left to right from the timetable to the sign 1 minute out.
-- Recomputed at most hourly (macros/hourly.sql).

{{ config(indexes=[{'columns': ['route_id']}, {'columns': ['stop_id']}], **hourly_config()) }}

{{ hourly_start() }}
with told as (
    select 'timetable' as basis, null::int as ahead_min, route_id, stop_id, delay_s as error_s
    from {{ ref('fct_stop_events') }}
    where status is not null
    union all
    select 'sign', horizon_min, route_id, stop_id, error_s
    from {{ ref('fct_prediction_errors') }}
    where horizon_min in (1, 2, 3, 5, 10, 15, 20, 30)
)

select
    basis, ahead_min,
    case when grouping(route_id) = 0 then route_id end as route_id,
    case when grouping(stop_id) = 0 then stop_id end as stop_id,
    count(*)                                                  as n,
    percentile_cont(0.5) within group (order by error_s)      as median_s,
    percentile_cont(0.1) within group (order by error_s)      as p10_s,
    percentile_cont(0.9) within group (order by error_s)      as p90_s,
    percentile_cont(0.5) within group (order by abs(error_s)) as median_abs_s,
    count(*) filter (where abs(error_s) <= 60)                 as n_within_1min,
    count(*) filter (where abs(error_s) <= 120)                as n_within_2min
from told
group by basis, ahead_min, grouping sets ((), (route_id), (stop_id))
{{ hourly_end() }}