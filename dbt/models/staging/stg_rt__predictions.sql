-- Every prediction the feed ever asserted, current and superseded, with the
-- interval [first_seen_at, last_seen_at] over which it was asserted.
--
-- LTD's feed gives a departure time at most stops and an arrival time only at some
-- (terminals). predicted_time takes whichever is present; for a rider waiting at a
-- stop the two are the same moment. LTD sends no delay field, and its scheduled_time
-- is NOT per stop (at non-timepoints it repeats the previous timepoint's time), so
-- delay is computed downstream against the interpolated schedule
-- (int_scheduled_stop_events), never against feed_scheduled_time.
select
    trip_id, start_date as service_date, stop_sequence, stop_id,
    arrival_time, departure_time, arrival_delay, departure_delay, schedule_relationship,
    coalesce(arrival_time, departure_time)   as predicted_time,
    coalesce(arrival_delay, departure_delay) as predicted_delay,
    scheduled_time as feed_scheduled_time,
    first_seen_at, last_seen_at, first_fetch_id,
    false as is_superseded
from {{ source('rt', 'prediction_current') }}
union all
select
    trip_id, start_date, stop_sequence, stop_id,
    arrival_time, departure_time, arrival_delay, departure_delay, schedule_relationship,
    coalesce(arrival_time, departure_time),
    coalesce(arrival_delay, departure_delay),
    scheduled_time,
    first_seen_at, last_seen_at, first_fetch_id,
    true
from {{ source('rt', 'prediction_history') }}