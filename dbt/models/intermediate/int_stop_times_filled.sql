-- Fill in the blank (non-timepoint) times by interpolating between the nearest
-- timepoints before and after, by distance along the shape when the feed gives
-- it, else by stop count. This is what trip planners do with the same data.

{{ config(
    indexes=[{'columns': ['feed_version_id', 'trip_id', 'stop_sequence'], 'unique': True}]
) }}

with st as (
    select
        feed_version_id, trip_id, stop_sequence, stop_id, is_timepoint,
        coalesce(arrival_seconds, departure_seconds) as known_seconds,
        shape_dist_traveled,
        row_number() over (partition by feed_version_id, trip_id order by stop_sequence) as rn
    from {{ ref('stg_gtfs__stop_times') }}
),

anchored as (
    select
        *,
        max(case when known_seconds is not null then rn end) over (
            partition by feed_version_id, trip_id order by stop_sequence
            rows between unbounded preceding and current row) as prev_rn,
        min(case when known_seconds is not null then rn end) over (
            partition by feed_version_id, trip_id order by stop_sequence
            rows between current row and unbounded following) as next_rn
    from st
),

joined as (
    select
        a.feed_version_id, a.trip_id, a.stop_sequence, a.stop_id, a.is_timepoint, a.known_seconds,
        a.rn, a.prev_rn, a.next_rn, a.shape_dist_traveled,
        p.known_seconds as prev_seconds, p.shape_dist_traveled as prev_dist,
        n.known_seconds as next_seconds, n.shape_dist_traveled as next_dist
    from anchored a
    left join anchored p on p.feed_version_id = a.feed_version_id and p.trip_id = a.trip_id and p.rn = a.prev_rn
    left join anchored n on n.feed_version_id = a.feed_version_id and n.trip_id = a.trip_id and n.rn = a.next_rn
)

select
    feed_version_id, trip_id, stop_sequence, stop_id, is_timepoint,
    known_seconds is not null as has_scheduled_time,
    case
        when known_seconds is not null then known_seconds
        when prev_seconds is null then next_seconds
        when next_seconds is null then prev_seconds
        when next_rn = prev_rn then prev_seconds
        when shape_dist_traveled is not null and prev_dist is not null and next_dist is not null and next_dist > prev_dist
            then round(prev_seconds + (next_seconds - prev_seconds) * (shape_dist_traveled - prev_dist) / (next_dist - prev_dist))::int
        else round(prev_seconds + (next_seconds - prev_seconds) * (rn - prev_rn)::numeric / (next_rn - prev_rn))::int
    end as arrival_seconds
from joined