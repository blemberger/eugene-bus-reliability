-- Headway adherence on frequent service, by route/direction/hour/weekday type.
-- bunched: the bus came less than half the scheduled headway after the previous
-- one; gapped: more than 1.5×. Both mean uneven service even if "on time".

select
    h.route_id, r.route_short_name, h.direction_id,
    case extract(isodow from h.service_date) when 6 then 'saturday' when 7 then 'sunday' else 'weekday' end as weekday_type,
    extract(hour from h.scheduled_arrival at time zone '{{ var("timezone") }}')::int as hour_local,
    count(*)                                                                    as n_headways,
    percentile_cont(0.5) within group (order by h.scheduled_headway_s)          as median_scheduled_headway_s,
    percentile_cont(0.5) within group (order by h.observed_headway_s)           as median_observed_headway_s,
    count(*) filter (where h.observed_headway_s < 0.5 * h.scheduled_headway_s)  as n_bunched,
    count(*) filter (where h.observed_headway_s > 1.5 * h.scheduled_headway_s)  as n_gapped,
    avg(abs(h.observed_headway_s - h.scheduled_headway_s))                      as mean_abs_headway_dev_s,
    min(h.service_date) as first_day, max(h.service_date) as last_day, count(distinct h.service_date) as n_days
from {{ ref('int_headways') }} h
-- route names from the schedule in force on each service date, not the newest one loaded
join {{ ref('int_feed_version_by_date') }} fv on fv.service_date = h.service_date
join {{ ref('stg_gtfs__routes') }} r
  on r.route_id = h.route_id
 and r.feed_version_id = fv.feed_version_id
where h.scheduled_headway_s <= {{ var('frequent_headway_seconds') }}
  and h.scheduled_headway_s > 0
group by 1, 2, 3, 4, 5