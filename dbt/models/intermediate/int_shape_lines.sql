-- One linestring per route shape, built from shapes.txt points in order.
-- Geometry (not geography) because ST_LineLocatePoint works on planar geometry;
-- at Eugene's scale the distortion is irrelevant for locating a point along a line.

{{ config(indexes=[{'columns': ['feed_version_id', 'shape_id'], 'unique': True}]) }}

with lines as (
    select
        feed_version_id,
        shape_id,
        ST_MakeLine(ST_SetSRID(ST_MakePoint(shape_pt_lon, shape_pt_lat), 4326) order by shape_pt_sequence) as line,
        count(*) as n_points
    from {{ source('gtfs', 'shapes') }}
    group by 1, 2
)

select feed_version_id, shape_id, line, n_points, ST_Length(line::geography) as length_m
from lines