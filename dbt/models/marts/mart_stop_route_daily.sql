-- Reliability at each stop, per route serving it, per service date (all stops, not
-- just timepoints: riders wait at every stop).

select
    stop_id, route_id, route_short_name, direction_id, service_date, weekday_type,
    count(*)                                           as n_events,
    count(*) filter (where status = 'on_time')         as n_on_time,
    count(*) filter (where status = 'early')           as n_early,
    count(*) filter (where status = 'late')            as n_late,
    percentile_cont(0.5)  within group (order by delay_s) as median_delay_s,
    percentile_cont(0.05) within group (order by delay_s) as p05_delay_s,
    percentile_cont(0.95) within group (order by delay_s) as p95_delay_s
from {{ ref('fct_stop_events') }}
where status is not null
group by 1, 2, 3, 4, 5, 6