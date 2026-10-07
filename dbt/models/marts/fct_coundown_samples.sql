-- What the countdown showed, one bus arrival at a time. For each scored stop arrival and each
-- whole minute from 1 to 15: the prediction on display while the countdown read that many
-- minutes (predicted arrival minus the moment, 1:00 to 1:59 reads "1 min"), and how far off it
-- was. One row per arrival and minute, so every bus arrival counts once at each distance.
--
-- Why not score every prediction row (fct_prediction_errors) directly: LTD sends a new value
-- whenever its estimate changes, so a bus running off schedule produces many more rows than a
-- bus running to time. Counting rows weights the troubled buses several times over, which
-- made the countdown look worse than riders see it, and comparing that with the timetable over
-- all arrivals compared different sets of buses.
--
-- in_comparison: the arrival had a countdown at 15 minutes out (so at practically every
-- distance below it). The site compares the countdown and the timetable on exactly these
-- arrivals, so both are measured on the same buses and the timetable has one value.
--
-- error_s: observed − predicted, positive = the bus came later than the countdown said.
-- schedule_error_s: observed − scheduled for the same arrival (the timetable's error).
-- Built incrementally (macros/incremental.sql), like fct_prediction_errors.

{{ config(
    materialized='incremental',
    incremental_strategy='delete+insert',
    unique_key='service_date',
    indexes=[
        {'columns': ['service_date']},
        {'columns': ['stop_id', 'ahead_min']},
        {'columns': ['route_id', 'ahead_min']},
    ]
) }}

with e as (
    select service_date, trip_id, stop_sequence, stop_id, route_id, is_timepoint, weekday_type,
           scheduled_arrival, observed_arrival, predicted_arrival, predicted_at,
           -- on display until it was replaced, but never past the bus actually arriving
           least(coalesce(predicted_until, observed_arrival), observed_arrival) as shown_until,
           error_s
    from {{ ref('fct_prediction_errors') }}
    where {{ recent_days() }}
),

-- the whole minutes each prediction was on display for: it read (predicted − t) for t from
-- predicted_at to shown_until, so from floor((predicted − predicted_at)/60) minutes down to
-- floor((predicted − shown_until)/60)
spans as (
    select e.*,
           floor(extract(epoch from predicted_arrival - predicted_at) / 60)::int as read_from,
           floor(extract(epoch from predicted_arrival - shown_until) / 60)::int as read_to
    from e
),

shown as (
    select s.*, m.ahead_min,
           row_number() over (
               partition by s.service_date, s.trip_id, s.stop_sequence, m.ahead_min
               order by s.predicted_at desc
           ) as latest
    from spans s
    cross join lateral generate_series(greatest(1, s.read_to), least(15, s.read_from)) as m(ahead_min)
),

one as (
    select * from shown where latest = 1
)

select
    service_date, trip_id, stop_sequence, stop_id, route_id, is_timepoint, weekday_type,
    ahead_min,
    -- the hour of the day it was showing that, in Eugene
    extract(hour from (predicted_arrival - make_interval(mins => ahead_min))
                      at time zone '{{ var("timezone") }}')::int as hour_local,
    error_s,
    extract(epoch from observed_arrival - scheduled_arrival)::int as schedule_error_s,
    max(ahead_min) over (partition by service_date, trip_id, stop_sequence) = 15 as in_comparison
from one