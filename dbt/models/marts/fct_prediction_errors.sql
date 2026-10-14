-- Every prediction the feed made, scored against what happened.
-- horizon_s: how far ahead the prediction was made (predicted arrival − time it
-- was first asserted). error_s: observed − predicted; positive = bus came later
-- than the sign said. Scored against the time fct_stop_events scores for the stop (when the bus
-- left: LTD's recorded departure at first stops and timepoints, our GPS time elsewhere), so
-- only stops it scores count, and both measures use the same time.

-- Built incrementally (macros/incremental.sql): each build recomputes the latest two service days.

{{ config(
    materialized='incremental',
    incremental_strategy='delete+insert',
    unique_key='service_date',
    indexes=[
        {'columns': ['service_date']},
        {'columns': ['route_id', 'horizon_min']},
        {'columns': ['stop_id', 'horizon_min']},
    ]
) }}

with preds as (
    select trip_id, service_date, stop_sequence, predicted_time as arrival_time, first_seen_at, last_seen_at,
           predicted_delay as arrival_delay
    from {{ ref('stg_rt__predictions') }}
    where predicted_time is not null
      and {{ recent_days() }}
),

obs as (
    select service_date, trip_id, stop_sequence, stop_id, route_id, direction_id,
           scheduled_arrival, scored_time as observed_arrival, uncertainty_s, is_timepoint
    from {{ ref('fct_stop_events') }}
    where status is not null and {{ recent_days() }}
)

select
    o.service_date,
    o.trip_id,
    o.stop_sequence,
    o.stop_id,
    o.route_id,
    o.direction_id,
    o.is_timepoint,
    o.scheduled_arrival,
    o.observed_arrival,
    p.arrival_time as predicted_arrival,
    p.first_seen_at as predicted_at,
    p.last_seen_at as predicted_until,
    extract(epoch from p.arrival_time - p.first_seen_at)::int as horizon_s,
    floor(extract(epoch from p.arrival_time - p.first_seen_at) / 60)::int as horizon_min,
    extract(epoch from o.observed_arrival - p.arrival_time)::int as error_s,
    extract(hour from p.first_seen_at at time zone '{{ var("timezone") }}')::int as hour_local,
    case extract(isodow from o.service_date)
        when 6 then 'saturday' when 7 then 'sunday' else 'weekday' end as weekday_type
from preds p
join obs o
  on o.trip_id = p.trip_id and o.service_date = p.service_date and o.stop_sequence = p.stop_sequence
-- Predictions made after the bus had already arrived, or absurdly far ahead, are not "the sign".
where p.first_seen_at < o.observed_arrival
  and p.arrival_time > p.first_seen_at - interval '60 seconds'
  and p.arrival_time < p.first_seen_at + interval '90 minutes'