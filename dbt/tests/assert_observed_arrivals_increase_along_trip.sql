-- Observed arrivals must not go backwards along a trip (they are derived from a
-- running maximum, so a failure here means the derivation is wrong).
select service_date, trip_id, stop_sequence
from (
    select service_date, trip_id, stop_sequence, observed_arrival,
           lag(observed_arrival) over (partition by service_date, trip_id order by stop_sequence) as prev
    from {{ ref('int_observed_arrivals') }}
) s
where prev is not null and observed_arrival < prev