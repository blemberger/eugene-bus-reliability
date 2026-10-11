-- One row per scheduled stop event in the collection period, with what was observed.
-- The fact table every reliability number is computed from. A stop event is scored
-- (status is set) only when its observed arrival is bounded (a report before and after
-- the stop), plausible (within the window in dbt_project.yml) and precise (uncertainty no
-- more than max_uncertainty_seconds, which a feed outage can exceed); delay_s and
-- methods_diff_s are left empty for unbounded arrivals, whose "time" is only the trip's
-- first report.
--
-- At a trip's first stop the time that matters is when the bus leaves, but our GPS time is
-- when it got there (often several minutes before it is due out, waiting at the stop). So
-- there, when LTD's feed recorded a departure for a trip we saw running, that departure is
-- the time scored (time_source 'ltd_departure'); everywhere else it is our GPS arrival
-- ('gps').

-- Built incrementally (macros/incremental.sql): each build recomputes the latest two service days.

{{ config(
    materialized='incremental',
    incremental_strategy='delete+insert',
    unique_key='service_date',
    indexes=[
        {'columns': ['service_date']},
        {'columns': ['route_id', 'service_date']},
        {'columns': ['stop_id', 'service_date']},
    ]
) }}

with sched as (
    select * from {{ ref('int_scheduled_stop_events') }}
    where service_date >= (select min(fetched_at at time zone '{{ var("timezone") }}')::date from {{ source('rt', 'fetch') }})
      and {{ recent_days() }}
      -- today's service date in Eugene; like the poller, the service day rolls over at 3 am
      and service_date <= ((now() at time zone '{{ var("timezone") }}') - interval '3 hours')::date
),

settled as (
    select service_date, trip_id, stop_sequence, settled_time, feed_scheduled_time, was_skipped
    from {{ ref('int_feed_settled_times') }} where is_settled and {{ recent_days() }}
),

obs as (
    select service_date, trip_id, stop_sequence, vehicle_id, observed_arrival, uncertainty_s,
           is_bounded, is_plausible
    from {{ ref('int_observed_arrivals') }}
    where {{ recent_days() }}
),

seen_trips as (
    select distinct trip_id, service_date from {{ ref('stg_rt__vehicle_positions') }}
    where trip_id is not null and service_date is not null and {{ recent_days() }}
      -- a day's reports start the evening before at the earliest (uses the timestamp index)
      and position_timestamp >= ({{ recent_start() }}::timestamp - interval '1 day') at time zone '{{ var("timezone") }}'
),

routes as (
    select feed_version_id, route_id, route_short_name, route_long_name from {{ ref('stg_gtfs__routes') }}
),

joined as (
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
        coalesce(
            -- was_skipped is null (not false) for a stop the feed made no remark about
            s.is_first_stop and f.settled_time is not null
            and not coalesce(f.was_skipped, false) and st.trip_id is not null
            and extract(epoch from f.settled_time - s.scheduled_arrival)
                between -{{ var('plausible_early_seconds') }} and {{ var('plausible_late_seconds') }},
            false
        ) as use_ltd_departure,
        o.is_bounded and o.is_plausible
            and o.uncertainty_s <= {{ var('max_uncertainty_seconds') }} as gps_scorable,
        s.scheduled_arrival as sched_time
    from sched s
    join routes r on r.route_id = s.route_id and r.feed_version_id = s.feed_version_id
    left join seen_trips st on st.trip_id = s.trip_id and st.service_date = s.service_date
    left join obs o on o.trip_id = s.trip_id and o.service_date = s.service_date and o.stop_sequence = s.stop_sequence
    left join settled f on f.trip_id = s.trip_id and f.service_date = s.service_date and f.stop_sequence = s.stop_sequence
),

scored as (
    select *,
        case when use_ltd_departure then feed_settled_time
             when gps_scorable then observed_arrival end as scored_time
    from joined
)

select
    service_date, trip_id, stop_sequence, stop_id, route_id, route_short_name, direction_id,
    is_timepoint, is_first_stop, is_last_stop, scheduled_arrival, hour_local, weekday_type,
    trip_had_realtime, vehicle_id, observed_arrival, uncertainty_s, is_bounded, is_plausible,
    is_precise, feed_settled_time, feed_scheduled_time, was_skipped, methods_diff_s,
    case when use_ltd_departure then 'ltd_departure' when observed_arrival is not null then 'gps' end
        as time_source,
    case when use_ltd_departure then extract(epoch from feed_settled_time - sched_time)::int
         when is_bounded then extract(epoch from observed_arrival - sched_time)::int end as delay_s,
    case
        when scored_time is null then null
        when extract(epoch from scored_time - sched_time) < -{{ var('early_seconds') }} then 'early'
        when extract(epoch from scored_time - sched_time) >  {{ var('late_seconds') }} then 'late'
        else 'on_time'
    end as status
from scored