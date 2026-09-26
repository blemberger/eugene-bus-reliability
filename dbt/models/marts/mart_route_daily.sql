-- Reliability by route, direction, service date and hour. Timepoints only, bounded
-- observations only. Small enough to filter and re-aggregate in the app.

select
    route_id, route_short_name, direction_id, service_date, weekday_type, hour_local,
    count(*)                                          as n_events,
    count(*) filter (where status = 'on_time')        as n_on_time,
    count(*) filter (where status = 'early')          as n_early,
    count(*) filter (where status = 'late')           as n_late,
    percentile_cont(0.5) within group (order by delay_s) as median_delay_s,
    percentile_cont(0.9) within group (order by delay_s) as p90_delay_s,
    percentile_cont(0.1) within group (order by delay_s) as p10_delay_s
from {{ ref('fct_stop_events') }}
where status is not null and is_timepoint
group by 1, 2, 3, 4, 5, 6