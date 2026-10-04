-- Where along its trip's shape each scheduled stop lies, as a fraction 0..1. The stops are
-- walked in order along the shape (macros/shape_walk.sql), each placed on the first pass of
-- the shape after the previous stop, within reach of the scheduled travel time: on routes
-- that use the same street twice (out and back, or a loop) the nearest point on the whole
-- shape could be the wrong pass. Stops still out of order, or more than 60 m from the shape,
-- are flagged and excluded from arrival matching.

{{ config(indexes=[{'columns': ['feed_version_id', 'trip_id', 'stop_sequence'], 'unique': True}]) }}

with trip_stops as (
    select
        st.feed_version_id, st.trip_id, st.stop_sequence, st.stop_id, t.shape_id,
        ST_Transform(s.geom::geometry, 32610) as pt,
        f.arrival_seconds
    from {{ ref('stg_gtfs__stop_times') }} st
    join {{ ref('stg_gtfs__trips') }} t on t.trip_id = st.trip_id and t.feed_version_id = st.feed_version_id
    join {{ ref('stg_gtfs__stops') }} s on s.stop_id = st.stop_id and s.feed_version_id = st.feed_version_id
    join {{ ref('int_stop_times_filled') }} f
      on f.feed_version_id = st.feed_version_id and f.trip_id = st.trip_id and f.stop_sequence = st.stop_sequence
),

per_trip as (
    select
        feed_version_id, trip_id, shape_id,
        array_agg(stop_sequence order by stop_sequence) as seqs,
        array_agg(stop_id order by stop_sequence) as stop_ids,
        array_agg(pt order by stop_sequence) as pts,
        -- scheduled times only set how far ahead each stop may be (reach), so any date works
        array_agg(timestamptz '2000-01-01 00:00+00' + make_interval(secs => coalesce(arrival_seconds, 0))
                  order by stop_sequence) as ts
    from trip_stops
    group by 1, 2, 3
),

walked as (
    select p.feed_version_id, p.trip_id, p.shape_id,
           p.seqs[w.idx] as stop_sequence, p.stop_ids[w.idx] as stop_id,
           w.frac, w.dist_m as dist_to_shape_m
    from per_trip p
    join {{ ref('int_shape_lines') }} l on l.shape_id = p.shape_id and l.feed_version_id = p.feed_version_id
    cross join lateral analytics.shape_walk(l.line_m, p.pts, p.ts) w
),

ordered as (
    select *,
           max(frac) over (partition by feed_version_id, trip_id order by stop_sequence
                           rows between unbounded preceding and 1 preceding) as prev_max_frac
    from walked
)

select
    feed_version_id, trip_id, stop_sequence, stop_id, shape_id, frac, dist_to_shape_m,
    (prev_max_frac is null or frac > prev_max_frac) and dist_to_shape_m < 60 as is_usable
from ordered