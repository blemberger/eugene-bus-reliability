-- Observed and scheduled headways: the time since the previous bus of the same
-- route and direction at the same stop. Used for frequent routes, where riders
-- don't consult a timetable and "on time" is the wrong measure; evenness is.

with obs as (
    select
        service_date, route_id, direction_id, stop_id, trip_id, stop_sequence,
        scheduled_arrival, observed_arrival,
        lag(observed_arrival) over (
            partition by service_date, route_id, direction_id, stop_id order by observed_arrival
        ) as prev_observed,
        lag(scheduled_arrival) over (
            partition by service_date, route_id, direction_id, stop_id order by scheduled_arrival
        ) as prev_scheduled
    from {{ ref('int_observed_arrivals') }}
    where is_bounded and is_plausible and uncertainty_s <= {{ var('max_uncertainty_seconds') }}
)

select
    service_date, route_id, direction_id, stop_id, trip_id, stop_sequence,
    scheduled_arrival, observed_arrival,
    extract(epoch from observed_arrival - prev_observed)   as observed_headway_s,
    extract(epoch from scheduled_arrival - prev_scheduled) as scheduled_headway_s
from obs
where prev_observed is not null and prev_scheduled is not null
  and scheduled_arrival > prev_scheduled