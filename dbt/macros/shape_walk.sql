{#
  Locating points along a route shape when the shape passes the same street more than once
  (an out-and-back detour such as route 98's "via LCC", or a loop such as route 12's).
  Taking the nearest point on the whole shape can then pick the wrong pass: a bus on the way
  out looks as if it is already on the way back, and every stop in between is "passed" at once.
  These functions instead walk along the shape: each point is placed on the FIRST pass of the
  shape, ahead of where the previous point was, that comes close to it. Shapes are in metres
  (EPSG:32610, UTM zone 10N) so distances and reach are physical.

  analytics.shape_first_pass(line, pt, lo, hi)  -> (frac, dist_m)
      Within the part of the line between fractions lo and hi: the nearest point on the first
      pass of the line that comes within 25 m of the closest the line gets to pt.
  analytics.shape_walk(line, pts[], ts[])       -> (idx, frac, dist_m) for each point
      Successive GPS positions of one trip. The search window starts a little behind the
      furthest point reached so far (GPS jitter) and reaches as far ahead as a bus could have
      travelled since the previous report (40 m/s, plus 300 m); if nothing on the route is
      within 100 m of that window (a long gap in reports), it searches the rest of the line.
#}
{% macro create_shape_walk_functions() %}
create or replace function analytics.shape_first_pass(
    line geometry, pt geometry, lo float8, hi float8, out frac float8, out dist_m float8
) language plpgsql immutable as $fn$
declare
    total float8 := ST_Length(line);
    sub geometry;
    dmin float8;
    seg_no int;
    seg geometry;
    before float8;
begin
    lo := greatest(least(lo, 1), 0);
    hi := greatest(least(hi, 1), 0);
    if hi - lo < 1e-9 or total = 0 then
        frac := lo;
        dist_m := ST_Distance(ST_LineInterpolatePoint(line, lo), pt);
        return;
    end if;
    sub := ST_LineSubstring(line, lo, hi);
    dmin := ST_Distance(sub, pt);
    -- the first pass: the first run of consecutive segments that come within 25 m of the
    -- closest approach; within it, the segment nearest the point
    with segs as (
        select s.path[1] as k, s.geom as g, ST_Distance(s.geom, pt) as d
        from ST_DumpSegments(sub) s
    ),
    first_in as (select min(k) as k0 from segs where d <= dmin + 25),
    first_out as (
        select min(k) as k1 from segs, first_in where k > first_in.k0 and d > dmin + 25
    )
    select segs.k, segs.g into seg_no, seg
    from segs, first_in, first_out
    where segs.k >= first_in.k0 and (first_out.k1 is null or segs.k < first_out.k1)
    order by segs.d, segs.k
    limit 1;
    select coalesce(sum(ST_Length(s.geom)), 0) into before
    from ST_DumpSegments(sub) s
    where s.path[1] < seg_no;
    frac := least(lo + (before + ST_LineLocatePoint(seg, pt) * ST_Length(seg)) / total, 1);
    dist_m := ST_Distance(seg, pt);
end
$fn$;

create or replace function analytics.shape_walk(
    line geometry, pts geometry[], ts timestamptz[]
) returns table (idx int, frac float8, dist_m float8)
language plpgsql immutable as $fn$
declare
    total float8 := ST_Length(line);
    furthest float8 := null;
    lo float8;
    hi float8;
    f float8;
    d float8;
begin
    for i in 1 .. coalesce(array_length(pts, 1), 0) loop
        if furthest is null then
            lo := 0;
            hi := 1;
        else
            lo := furthest - 100 / total;
            hi := furthest + (300 + 40 * greatest(extract(epoch from ts[i] - ts[i - 1]), 0)) / total;
        end if;
        select p.frac, p.dist_m into f, d from analytics.shape_first_pass(line, pts[i], lo, hi) p;
        if d > 100 and furthest is not null and hi < 1 then
            select p.frac, p.dist_m into f, d from analytics.shape_first_pass(line, pts[i], lo, 1) p;
        end if;
        idx := i;
        frac := f;
        dist_m := d;
        return next;
        if d <= 100 and (furthest is null or f > furthest) then
            furthest := f;
        end if;
    end loop;
end
$fn$;
{% endmacro %}