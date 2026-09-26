-- How complete is each day's collection? Feed gaps, trips seen, stop events observed.

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
        count(distinct trip_id) filter (where trip_had_realtime) as trips_seen
    from {{ ref('fct_stop_events') }}
    group by 1
),

vehicles as (
    select service_date, count(distinct vehicle_id) as vehicles_reporting
    from {{ ref('stg_rt__vehicle_positions') }}
    where service_date is not null
    group by 1
)

select
    e.service_date,
    coalesce(f.trip_update_fetches, 0) as trip_update_fetches,
    coalesce(f.vehicle_fetches, 0)     as vehicle_fetches,
    coalesce(f.gap_minutes, 0)         as gap_minutes,
    v.vehicles_reporting,
    e.trips_scheduled, e.trips_seen,
    e.stop_events_scheduled, e.stop_events_trip_seen, e.stop_events_observed,
    e.stop_events_implausible,
    e.stop_events_imprecise
from events e
left join fetch_day f using (service_date)
left join vehicles v using (service_date)