-- LTD's feed gives arrival/departure times only at timepoints (about a third of
-- stop events); the rest are blank, as GTFS permits, and are meant to be
-- interpolated. int_stop_times_filled does that. A stop is a timepoint if the feed
-- flags it as one or, failing a flag, if it has a time at all.
select
    feed_version_id,
    trip_id,
    stop_sequence,
    stop_id,
    arrival_seconds,
    departure_seconds,
    coalesce(timepoint, case when arrival_seconds is null then 0 else 1 end) = 1 as is_timepoint,
    pickup_type,
    drop_off_type,
    shape_dist_traveled
from {{ source('gtfs', 'stop_times') }}