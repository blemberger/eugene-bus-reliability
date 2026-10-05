-- How late the buses are against the timetable, ready for the site's pages: every
-- combination of the pages' filters (period: last 7 days, last 30 days, all data; days: all,
-- weekdays, Saturdays, Sundays, or one weekday) at the levels the pages show. Medians can't
-- be added up from smaller pieces, so they are computed here instead of on every page view.
--
-- Levels (a null route_id / stop_id / hour_local means "all of them"): everything, by hour,
-- by route, by route and hour, by stop. Every scored arrival at every stop counts.
-- Periods count back from today in Eugene, as the pages do (page_filters in app/common.py):
-- "last 7 days" is today and the 6 days before it. `since` is the first day each row covers.
-- Recomputed at most hourly (macros/hourly.sql).

{{ config(indexes=[{'columns': ['period', 'days']}], **hourly_config()) }}

{{ hourly_start() }}
with ev as (
    select service_date, weekday_type, extract(isodow from service_date)::int as dow,
           route_id, route_short_name, stop_id, hour_local, status, delay_s
    from {{ ref('fct_stop_events') }}
    where status is not null
),

today as (
    select (now() at time zone '{{ var("timezone") }}')::date as d
),

periods as (
    select '7d' as period, d - 6 as since from today
    union all select '30d', d - 29 from today
    union all select 'all', (select min(service_date) from ev)
),

expanded as (
    select p.period, p.since, dd.days, e.*
    from ev e
    join periods p on e.service_date >= p.since
    cross join lateral (
        values ('all'), (e.weekday_type), (case when e.weekday_type = 'weekday' then 'dow' || e.dow end)
    ) as dd (days)
    where dd.days is not null
)

select
    period, since, days,
    case when grouping(route_id) = 0 then route_id end as route_id,
    case when grouping(route_id) = 0 then max(route_short_name) end as route_short_name,
    case when grouping(stop_id) = 0 then stop_id end as stop_id,
    case when grouping(stop_id) = 0 then string_agg(distinct route_short_name, ', ') end as routes_here,
    case when grouping(hour_local) = 0 then hour_local end as hour_local,
    count(*)                                                as n,
    count(*) filter (where status = 'early')                as n_early,
    count(*) filter (where status = 'on_time')              as n_on_time,
    count(*) filter (where status = 'late')                 as n_late,
    percentile_cont(0.5) within group (order by delay_s)    as median_delay_s,
    percentile_cont(0.1) within group (order by delay_s)    as p10_delay_s,
    percentile_cont(0.9) within group (order by delay_s)    as p90_delay_s,
    count(distinct service_date)                            as n_days
from expanded
group by period, since, days,
    grouping sets ((), (hour_local), (route_id), (route_id, hour_local), (stop_id))
{{ hourly_end() }}