-- "How close to what you were told did the bus come?", for each thing that tells you:
--   basis 'timetable'  the printed schedule (ahead_min null): actual − scheduled arrival;
--   basis 'sign'       LTD's countdown when it read ahead_min minutes (1 to 15): actual −
--                      the arrival it was showing (fct_countdown_samples).
-- Both on the same bus arrivals: those that had a countdown at 15 minutes out
-- (fct_countdown_samples.in_comparison), each arrival counted once per row. So the timetable
-- has one value, and the countdown at each distance is measured on the same buses as it.
-- Positive = the bus came later than you were told. Rows for all routes and stops together
-- (route_id and stop_id null), per route, and per stop. All data. Used by the Countdown page
-- (the bars, the "how often is it right" lines, the headline numbers and the routes table).
-- Recomputed at most hourly (macros/hourly.sql).

{{ config(indexes=[{'columns': ['route_id']}, {'columns': ['stop_id']}], **hourly_config()) }}

{{ hourly_start() }}
with told as (
    select 'timetable' as basis, null::int as ahead_min, route_id, stop_id, service_date,
           schedule_error_s as error_s
    from {{ ref('fct_countdown_samples') }}
    where in_comparison and ahead_min = 15
    union all
    select 'sign', ahead_min, route_id, stop_id, service_date, error_s
    from {{ ref('fct_countdown_samples') }}
    where in_comparison
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
    count(*) filter (where abs(error_s) <= 120)                as n_within_2min,
    -- where the rest fell: more than a minute early, 1 to 3 minutes late, more than 3 late
    count(*) filter (where error_s < -60)                      as n_early_1min,
    count(*) filter (where error_s > 60 and error_s <= 180)    as n_late_1_3min,
    count(*) filter (where error_s > 180)                      as n_late_3min,
    min(service_date)                                          as first_day,
    max(service_date)                                          as last_day,
    count(distinct service_date)                               as n_days
from told
group by basis, ahead_min, grouping sets ((), (route_id), (stop_id))
{{ hourly_end() }}