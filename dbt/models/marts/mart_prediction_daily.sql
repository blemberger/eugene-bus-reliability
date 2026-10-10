-- How far off LTD's real-time predictions were, and the printed timetable for the same bus
-- arrivals, added up per service day, route, minutes away and hour of day, so the Predictions
-- page can follow any period and days of the week by summing rows (an average is
-- sum_abs_s / n). From fct_countdown_samples, on the arrivals that had a prediction 15 minutes
-- out (in_comparison), each counted once per minute away:
--   basis 'sign'       the prediction while it said ahead_min minutes (1 to 15);
--   basis 'timetable'  the printed timetable for those arrivals (ahead_min null), counted once
--                      per arrival (from its 15-minute sample).
-- hour_local: the hour the prediction was on show (for the timetable, 15 minutes before the
-- predicted arrival). Built incrementally (macros/incremental.sql), like its source.

{{ config(
    materialized='incremental',
    incremental_strategy='delete+insert',
    unique_key='service_date',
    indexes=[{'columns': ['service_date']}],
) }}

with told as (
    select service_date, weekday_type, route_id, 'sign' as basis, ahead_min, hour_local, error_s
    from {{ ref('fct_countdown_samples') }}
    where in_comparison and {{ recent_days() }}
    union all
    select service_date, weekday_type, route_id, 'timetable', null::int, hour_local,
           schedule_error_s
    from {{ ref('fct_countdown_samples') }}
    where in_comparison and ahead_min = 15 and {{ recent_days() }}
)

select
    service_date, weekday_type, route_id, basis, ahead_min, hour_local,
    count(*)                                    as n,
    sum(abs(error_s))::bigint                   as sum_abs_s,
    count(*) filter (where abs(error_s) <= 60)  as n_within_1min,
    count(*) filter (where abs(error_s) <= 120) as n_within_2min,
    count(*) filter (where abs(error_s) <= 180) as n_within_3min
from told
group by 1, 2, 3, 4, 5, 6