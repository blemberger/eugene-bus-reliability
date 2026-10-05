-- Interpolated times must not go backwards along a trip.
select feed_version_id, trip_id, stop_sequence
from (
    select feed_version_id, trip_id, stop_sequence, arrival_seconds,
           lag(arrival_seconds) over (partition by feed_version_id, trip_id order by stop_sequence) as prev
    from {{ ref('int_stop_times_filled') }}
) s
where prev is not null and arrival_seconds < prev