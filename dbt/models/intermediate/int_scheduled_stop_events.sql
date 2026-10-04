-- One row per scheduled stop event: every (trip, stop_sequence) on every day the
-- trip's service runs, with the scheduled arrival as a real timestamptz.
--
-- GTFS times are seconds after "noon minus 12 hours" on the service date, so a
-- 24:05:00 arrival is 00:05 the next calendar day and DST transitions are
-- handled by anchoring at noon, not midnight. Times at non-timepoints come from
-- int_stop_times_filled (interpolated). This is the spine every realtime
-- observation is matched to.

-- Built incrementally (macros/incremental.sql): each build recomputes the latest two service days.

{{ config(
    materialized='incremental',
    incremental_strategy='delete+insert',
    unique_key='service_date',
    indexes=[
        {'columns': ['service_date', 'trip_id', 'stop_sequence'], 'unique': True},
        {'columns': ['scheduled_arrival']},
        {'columns': ['stop_id', 'scheduled_arrival']},
    ]
) }}

with active as (
    select * from {{ ref('int_active_services') }}
    where {{ recent_days() }}
),

trips as (
    select * from {{ ref('stg_gtfs__trips') }}
),

stop_times as (
    select * from {{ ref('int_stop_times_filled') }}
)

select
    a.service_date,
    a.feed_version_id,
    t.trip_id,
    t.route_id,
    t.direction_id,
    st.stop_sequence,
    st.stop_id,
    st.is_timepoint,
    st.has_scheduled_time,
    ((a.service_date::timestamp + interval '12 hours') at time zone '{{ var("timezone") }}')
        - interval '12 hours'
        + make_interval(secs => st.arrival_seconds)   as scheduled_arrival,
    st.stop_sequence = min(st.stop_sequence) over (partition by a.service_date, t.trip_id) as is_first_stop,
    st.stop_sequence = max(st.stop_sequence) over (partition by a.service_date, t.trip_id) as is_last_stop
from active a
join trips t
  on t.feed_version_id = a.feed_version_id
 and t.service_id = a.service_id
join stop_times st
  on st.feed_version_id = t.feed_version_id
 and st.trip_id = t.trip_id