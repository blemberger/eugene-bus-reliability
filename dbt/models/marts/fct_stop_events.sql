-- One row per scheduled stop event in the collection period, with what was observed.
-- The fact table every reliability number is computed from. A stop event is scored
-- (status is set) only when its observed arrival is bounded (a report before and after
-- the stop), plausible (within the window in dbt_project.yml) and precise (uncertainty no
-- more than max_uncertainty_seconds, which a feed outage can exceed); delay_s and
-- methods_diff_s are left empty for unbounded arrivals, whose "time" is only the trip's
-- first report.
--
-- The time scored is when the bus left the stop. At a trip's first stop and at timepoints,
-- LTD's feed records a departure time itself, and each of the two times can come before the
-- bus really left: our GPS time can catch a bus that is waiting (at the first stop for minutes
-- before it is due out, at a timepoint when it is early and holding), and LTD's record can be
-- early too (a quarter of LTD's timepoint departures are exactly the scheduled time, and the
-- stops between timepoints ran 47 s later than LTD's timepoint times but 1 s later than our
-- GPS ones: dump, BETWEEN-TIMEPOINT CHECK). So there the later of the two is scored (each
-- alone when it is the only one), and time_source says which it was ('ltd_departure' or
-- 'gps'); everywhere else it is our GPS time ('gps'), except at stops where our GPS time is
-- doubtful as a rule (int_doubtful_stops), which are not scored.

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

doubtful as (
    select route_id, direction_id, stop_id from {{ ref('int_doubtful_stops') }}
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
            (s.is_first_stop or s.is_timepoint) and f.settled_time is not null
            and not coalesce(f.was_skipped, false) and st.trip_id is not null
            and extract(epoch from f.settled_time - s.scheduled_arrival)
                between -{{ var('plausible_early_seconds') }} and {{ var('plausible_late_seconds') }},
            false
        ) as use_ltd_departure,
        d.stop_id is not null as is_doubtful,
        o.is_bounded and o.is_plausible and d.stop_id is null
            and o.uncertainty_s <= {{ var('max_uncertainty_seconds') }} as gps_scorable,
        s.scheduled_arrival as sched_time
    from sched s
    join routes r on r.route_id = s.route_id and r.feed_version_id = s.feed_version_id
    left join seen_trips st on st.trip_id = s.trip_id and st.service_date = s.service_date
    left join obs o on o.trip_id = s.trip_id and o.service_date = s.service_date and o.stop_sequence = s.stop_sequence
    left join settled f on f.trip_id = s.trip_id and f.service_date = s.service_date and f.stop_sequence = s.stop_sequence
    left join doubtful d on d.route_id = s.route_id and d.stop_id = s.stop_id
     and d.direction_id is not distinct from s.direction_id
),

picked as (
    select *,
        -- LTD's departure is the time scored where it is the only one, or the later one
        -- (coalesce: with no GPS time for the stop, gps_scorable is null, not false)
        use_ltd_departure
            and not coalesce(gps_scorable and observed_arrival > feed_settled_time, false)
            as ltd_scored
    from joined
),

scored as (
    select *,
        case when ltd_scored then feed_settled_time
             when gps_scorable then observed_arrival end as scored_time
    from picked
)

select
    service_date, trip_id, stop_sequence, stop_id, route_id, route_short_name, direction_id,
    is_timepoint, is_first_stop, is_last_stop, scheduled_arrival, hour_local, weekday_type,
    trip_had_realtime, vehicle_id, observed_arrival, uncertainty_s, is_bounded, is_plausible,
    is_precise, is_doubtful, feed_settled_time, feed_scheduled_time, was_skipped, methods_diff_s,
    -- when the bus left, as scored (status is set): LTD's departure or our GPS time
    case when scored_time is not null then scored_time end as scored_time,
    case when ltd_scored then 'ltd_departure' when observed_arrival is not null then 'gps' end
        as time_source,
    case when ltd_scored then extract(epoch from feed_settled_time - sched_time)::int
         when is_bounded and not is_doubtful then extract(epoch from observed_arrival - sched_time)::int
    end as delay_s,
    case
        when scored_time is null then null
        when extract(epoch from scored_time - sched_time) < -{{ var('early_seconds') }} then 'early'
        when extract(epoch from scored_time - sched_time) >  {{ var('late_seconds') }} then 'late'
        else 'on_time'
    end as status
from scored