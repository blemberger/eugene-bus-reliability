-- One linestring per route shape, built from shapes.txt points in order: in longitude and
-- latitude (line, for maps) and in metres (line_m, UTM zone 10N, for locating stops and buses
-- along it; see macros/shape_walk.sql).

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

select feed_version_id, shape_id, line, ST_Transform(line, 32610) as line_m, n_points,
       ST_Length(line::geography) as length_m
from lines