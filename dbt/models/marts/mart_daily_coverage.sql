-- How complete is each day's collection? Feed gaps, trips seen, stop events observed.
--
-- The two completeness measures, both ideally 100%, count only stop events due more than
-- two hours before the build (later ones may simply not have happened yet):
--   trips_reported / trips_due: trips LTD's feed reported at all. A shortfall is LTD's side
--     (a cancelled trip, a bus not reporting) or our own collection outage (gap_minutes).
--   mid_stops_timed / mid_stops_due: on those trips, the stops we timed, leaving out each
--     trip's first and last stop. The first can't be timed by our method (the bus sits there
--     and starts reporting the trip already at the stop) and at the last the bus often switches
--     to its next trip before passing it, so including them would cap the measure near 93%.
--     A shortfall here is our measurement missing stops: GPS gaps, detours, a trip that started
--     reporting part-way along.
-- Built incrementally (macros/incremental.sql): each build recomputes the latest two service days.

{{ config(materialized='incremental', incremental_strategy='delete+insert', unique_key='service_date') }}


with fetches as (
    select
        (fetched_at at time zone '{{ var("timezone") }}')::date as service_date,
        feed,
        count(*) as n_fetches,
        sum(case when gap_s > 120 then gap_s else 0 end) / 60.0 as gap_minutes
    from (
        select feed, fetched_at,
               extract(epoch from fetched_at - lag(fetched_at) over (partition by feed order by fetched_at)) as gap_s
        from {{ source('rt', 'fetch') }}
        -- from a day before the first day computed, so that day's first gap is measured too
        where fetched_at >= ({{ recent_start() }}::timestamp - interval '1 day') at time zone '{{ var("timezone") }}'
    ) f
    where feed in ('trip_updates', 'vehicle_positions')
    group by 1, 2
),

fetch_day as (
    select service_date,
           sum(n_fetches) filter (where feed = 'trip_updates')      as trip_update_fetches,
           sum(n_fetches) filter (where feed = 'vehicle_positions') as vehicle_fetches,
           max(gap_minutes)                                         as gap_minutes
    from fetches group by 1
),

events as (
    select
        service_date,
        count(*)                                        as stop_events_scheduled,
        count(*) filter (where trip_had_realtime)       as stop_events_trip_seen,
        count(*) filter (where status is not null)      as stop_events_observed,
        count(*) filter (where is_bounded and not is_plausible) as stop_events_implausible,
        count(*) filter (where is_bounded and is_plausible and not is_precise) as stop_events_imprecise,
        count(distinct trip_id)                         as trips_scheduled,
        count(distinct trip_id) filter (where trip_had_realtime) as trips_seen,
        count(*) filter (where is_first_stop and due)                       as trips_due,
        count(*) filter (where is_first_stop and due and trip_had_realtime) as trips_reported,
        count(*) filter (where mid_trip and due and trip_had_realtime)      as mid_stops_due,
        count(*) filter (where mid_trip and due and trip_had_realtime and status is not null)
                                                        as mid_stops_timed
    from (
        select *,
               scheduled_arrival < now() - interval '2 hours' as due,
               not is_first_stop and not is_last_stop as mid_trip
        from {{ ref('fct_stop_events') }}
        where {{ recent_days() }}
    ) e
    group by 1
),

vehicles as (
    select service_date, count(distinct vehicle_id) as vehicles_reporting
    from {{ ref('stg_rt__vehicle_positions') }}
    where service_date is not null and {{ recent_days() }}
      and position_timestamp >= ({{ recent_start() }}::timestamp - interval '1 day') at time zone '{{ var("timezone") }}'
    group by 1
)

select
    e.service_date,
    coalesce(f.trip_update_fetches, 0) as trip_update_fetches,
    coalesce(f.vehicle_fetches, 0)     as vehicle_fetches,
    coalesce(f.gap_minutes, 0)         as gap_minutes,
    v.vehicles_reporting,
    e.trips_scheduled, e.trips_seen,
    e.trips_due, e.trips_reported, e.mid_stops_due, e.mid_stops_timed,
    e.stop_events_scheduled, e.stop_events_trip_seen, e.stop_events_observed,
    e.stop_events_implausible,
    e.stop_events_imprecise
from events e
left join fetch_day f using (service_date)
left join vehicles v using (service_date)