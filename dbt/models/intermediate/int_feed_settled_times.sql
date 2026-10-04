-- The feed's own record of when a bus left each stop: for stops already passed,
-- LTD keeps reporting a departure time that no longer changes and lies in the
-- past. The final value asserted for a trip-stop, if it was still being asserted
-- after the moment it refers to, is taken as the feed's settled time. This is
-- independent of the position-based derivation and is used to check it.

-- Built incrementally (macros/incremental.sql): each build recomputes the latest two service days.

{{ config(
    materialized='incremental',
    incremental_strategy='delete+insert',
    unique_key='service_date',
    indexes=[{'columns': ['service_date', 'trip_id', 'stop_sequence'], 'unique': True}]
) }}

with final_value as (
    select distinct on (trip_id, service_date, stop_sequence)
           trip_id, service_date, stop_sequence, stop_id, predicted_time, feed_scheduled_time,
           first_seen_at, last_seen_at, schedule_relationship
    from {{ ref('stg_rt__predictions') }}
    where predicted_time is not null
      and {{ recent_days() }}
    order by trip_id, service_date, stop_sequence, first_seen_at desc
)

select
    trip_id, service_date, stop_sequence, stop_id,
    predicted_time as settled_time,
    feed_scheduled_time,
    schedule_relationship = 1 as was_skipped,
    last_seen_at >= predicted_time as is_settled,
    extract(epoch from last_seen_at - predicted_time) as held_after_s
from final_value