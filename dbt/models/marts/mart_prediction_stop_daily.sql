-- mart_prediction_daily, but per stop and direction as well: how far off LTD's real-time
-- predictions were at each stop on each route, and the printed timetable for the same bus
-- arrivals, added up per service day. The stop and route report cards sum these rows for any
-- period and days of the week (an average is sum_abs_s / n), instead of scanning
-- fct_countdown_samples. Same rows and rules as mart_prediction_daily (in_comparison
-- arrivals; basis 'sign' per minute away 1 to 15, basis 'timetable' once per arrival).
-- Built incrementally (macros/incremental.sql), like its source.

{{ config(
    materialized='incremental',
    incremental_strategy='delete+insert',
    unique_key='service_date',
    indexes=[
        {'columns': ['service_date']},
        {'columns': ['stop_id', 'service_date']},
        {'columns': ['route_id', 'service_date']},
    ],
) }}

with c as (
    select c.service_date, c.weekday_type, c.route_id, e.direction_id, c.stop_id, c.ahead_min,
           c.error_s, c.schedule_error_s
    from {{ ref('fct_countdown_samples') }} c
    join {{ ref('fct_stop_events') }} e
      on e.service_date = c.service_date and e.trip_id = c.trip_id
     and e.stop_sequence = c.stop_sequence
    where c.in_comparison and {{ recent_days('c.service_date') }}
      and {{ recent_days('e.service_date') }}
),

told as (
    select service_date, weekday_type, route_id, direction_id, stop_id, 'sign' as basis,
           ahead_min, error_s
    from c
    union all
    select service_date, weekday_type, route_id, direction_id, stop_id, 'timetable', null::int,
           schedule_error_s
    from c where ahead_min = 15
)

select
    service_date, weekday_type, route_id, direction_id, stop_id, basis, ahead_min,
    count(*)                  as n,
    sum(abs(error_s))::bigint as sum_abs_s
from told
group by 1, 2, 3, 4, 5, 6, 7