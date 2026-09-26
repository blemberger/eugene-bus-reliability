-- One row per scheduled stop event in the collection period, with what was observed.
-- The fact table every reliability number is computed from. A stop event is scored
-- (status is set) only when its observed arrival is bounded (a report before and after
-- the stop), plausible (within the window in dbt_project.yml) and precise (uncertainty no
-- more than max_uncertainty_seconds, which a feed outage can exceed); delay_s and
-- methods_diff_s are left empty for unbounded arrivals, whose "time" is only the trip's
-- first report.

{{ config(
    indexes=[
        {'columns': ['service_date']},
        {'columns': ['route_id', 'service_date']},
        {'columns': ['stop_id', 'service_date']},
    ]
) }}

with sched as (
    select * from {{ ref('int_scheduled_stop_events') }}
    where service_date >= (select min(fetched_at at time zone '{{ var("timezone") }}')::date from {{ source('rt', 'fetch') }})
      and service_date >= current_date - {{ var('lookback_days') }}
      -- today's service date in Eugene; like the poller, the service day rolls over at 3 am
      and service_date <= ((now() at time zone '{{ var("timezone") }}') - interval '3 hours')::date
),

settled as (
    select service_date, trip_id, stop_sequence, settled_time, feed_scheduled_time, was_skipped
    from {{ ref('int_feed_settled_times') }} where is_settled
),

obs as (
    select service_date, trip_id, stop_sequence, vehicle_id, observed_arrival, uncertainty_s,
           is_bounded, is_plausible
    from {{ ref('int_observed_arrivals') }}
),

seen_trips as (
    select distinct trip_id, service_date from {{ ref('stg_rt__vehicle_positions') }}
    where trip_id is not null and service_date is not null
),

routes as (
    select feed_version_id, route_id, route_short_name, route_long_name from {{ ref('stg_gtfs__routes') }}
)

select
    s.service_date,
    s.trip_id,
    s.stop_sequence,
    s.stop_id,
    s.route_id,
    r.route_short_name,
    s.direction_id,
    s.is_timepoint,
    s.is_first_stop,
    s.is_last_stop,
    s.scheduled_arrival,
    extract(hour from s.scheduled_arrival at time zone '{{ var("timezone") }}')::int as hour_local,
    case extract(isodow from s.service_date)
        when 6 then 'saturday' when 7 then 'sunday' else 'weekday' end as weekday_type,
    st.trip_id is not null as trip_had_realtime,
    o.vehicle_id,
    o.observed_arrival,
    o.uncertainty_s,
    coalesce(o.is_bounded, false) as is_bounded,
    coalesce(o.is_plausible, false) as is_plausible,
    coalesce(o.uncertainty_s <= {{ var('max_uncertainty_seconds') }}, false) as is_precise,
    f.settled_time as feed_settled_time,
    f.feed_scheduled_time,
    coalesce(f.was_skipped, false) as was_skipped,
    case when o.is_bounded then extract(epoch from f.settled_time - o.observed_arrival)::int end as methods_diff_s,
    case when o.is_bounded then extract(epoch from o.observed_arrival - s.scheduled_arrival)::int end as delay_s,
    case
        when o.observed_arrival is null or not o.is_bounded or not o.is_plausible
             or o.uncertainty_s > {{ var('max_uncertainty_seconds') }} then null
        when extract(epoch from o.observed_arrival - s.scheduled_arrival) < -{{ var('early_seconds') }} then 'early'
        when extract(epoch from o.observed_arrival - s.scheduled_arrival) >  {{ var('late_seconds') }} then 'late'
        else 'on_time'
    end as status
from sched s
join routes r on r.route_id = s.route_id and r.feed_version_id = s.feed_version_id
left join seen_trips st on st.trip_id = s.trip_id and st.service_date = s.service_date
left join obs o on o.trip_id = s.trip_id and o.service_date = s.service_date and o.stop_sequence = s.stop_sequence
left join settled f on f.trip_id = s.trip_id and f.service_date = s.service_date and f.stop_sequence = s.stop_sequence