-- How accurate is the sign, as a function of how far out it is? Rows with
-- route_id null are the system-wide totals (GROUPING SETS gives both in one pass).
--
-- Each horizon also carries a baseline: for the same stop events, how often the
-- timetable alone would have been as close. Where the prediction's accuracy falls to
-- the timetable's, the prediction adds nothing; the gap between the two lines is what
-- the realtime system is worth at that horizon.
--
-- Horizons run to 60 minutes. That limit is ours: LTD publishes a time for every
-- remaining stop of a trip in progress, so its horizons reach the length of a trip, but
-- beyond an hour there are few predictions per horizon and they are timetable-like.

select
    case when grouping(route_id) = 1 then null else route_id end as route_id,
    horizon_min,
    count(*)                                                  as n_predictions,
    count(*) filter (where abs(error_s) <= 60)                as n_within_1min,
    count(*) filter (where abs(error_s) <= 120)               as n_within_2min,
    count(*) filter (where abs(error_s) <= 300)               as n_within_5min,
    count(*) filter (where error_s < -60)                     as n_bus_earlier_than_sign,
    count(*) filter (where error_s > 60)                      as n_bus_later_than_sign,
    percentile_cont(0.5) within group (order by error_s)      as median_error_s,
    avg(error_s)                                              as mean_error_s,
    percentile_cont(0.1) within group (order by error_s)      as p10_error_s,
    percentile_cont(0.9) within group (order by error_s)      as p90_error_s,
    -- the timetable as a forecast, for the same stop events
    count(*) filter (where abs(schedule_error_s) <= 60)       as n_schedule_within_1min,
    count(*) filter (where abs(schedule_error_s) <= 120)      as n_schedule_within_2min,
    count(*) filter (where abs(schedule_error_s) <= 300)      as n_schedule_within_5min,
    min(service_date)                                         as first_day,
    max(service_date)                                         as last_day,
    count(distinct service_date)                              as n_days
from (
    select *, extract(epoch from observed_arrival - scheduled_arrival)::int as schedule_error_s
    from {{ ref('fct_prediction_errors') }}
) e
where horizon_min between 0 and 60
group by grouping sets ((horizon_min), (route_id, horizon_min))