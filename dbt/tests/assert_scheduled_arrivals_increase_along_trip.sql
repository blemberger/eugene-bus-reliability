-- Singular test: within a trip on a day, scheduled arrival must not decrease
-- with stop_sequence. A failure means a GTFS time was mis-parsed or the
-- service-day anchoring is wrong.
select service_date, trip_id, stop_sequence
from (
    select
        service_date, trip_id, stop_sequence, scheduled_arrival,
        lag(scheduled_arrival) over (partition by service_date, trip_id order by stop_sequence) as prev
    from {{ ref('int_scheduled_stop_events') }}
    where service_date >= current_date - 3  -- the days a build recomputes; older ones were checked when built
) s
where prev is not null and scheduled_arrival < prev