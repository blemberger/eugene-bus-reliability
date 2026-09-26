-- Where along its trip's shape each scheduled stop lies, as a fraction 0..1.
-- Stops whose fraction does not increase with stop_sequence (a shape that loops
-- back past an earlier point) are flagged and excluded from arrival matching.

{{ config(indexes=[{'columns': ['feed_version_id', 'trip_id', 'stop_sequence'], 'unique': True}]) }}

with located as (
    select
        st.feed_version_id, st.trip_id, st.stop_sequence, st.stop_id, t.shape_id,
        ST_LineLocatePoint(l.line, s.geom::geometry) as frac,
        ST_Distance(l.line::geography, s.geom) as dist_to_shape_m
    from {{ ref('stg_gtfs__stop_times') }} st
    join {{ ref('stg_gtfs__trips') }} t on t.trip_id = st.trip_id and t.feed_version_id = st.feed_version_id
    join {{ ref('int_shape_lines') }} l on l.shape_id = t.shape_id and l.feed_version_id = t.feed_version_id
    join {{ ref('stg_gtfs__stops') }} s on s.stop_id = st.stop_id and s.feed_version_id = st.feed_version_id
),

ordered as (
    select *,
           max(frac) over (partition by feed_version_id, trip_id order by stop_sequence
                           rows between unbounded preceding and 1 preceding) as prev_max_frac
    from located
)

select
    feed_version_id, trip_id, stop_sequence, stop_id, shape_id, frac, dist_to_shape_m,
    (prev_max_frac is null or frac > prev_max_frac) and dist_to_shape_m < 60 as is_usable
from ordered