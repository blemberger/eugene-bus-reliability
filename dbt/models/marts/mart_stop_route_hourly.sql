-- Same, by hour of day and weekday type across all dates: the "when should I get
-- to my stop" table. p05_delay_s is how early the earliest 5% of buses run; arriving
-- that many seconds before the scheduled time catches 95% of them.

select
    stop_id, route_id, route_short_name, direction_id, weekday_type, hour_local,
    count(*)                                           as n_events,
    count(*) filter (where status = 'on_time')         as n_on_time,
    count(*) filter (where status = 'early')           as n_early,
    count(*) filter (where status = 'late')            as n_late,
    percentile_cont(0.5)  within group (order by delay_s) as median_delay_s,
    percentile_cont(0.05) within group (order by delay_s) as p05_delay_s,
    percentile_cont(0.95) within group (order by delay_s) as p95_delay_s,
    min(service_date)                                     as first_day,
    max(service_date)                                     as last_day,
    count(distinct service_date)                          as n_days
from {{ ref('fct_stop_events') }}
where status is not null
group by 1, 2, 3, 4, 5, 6