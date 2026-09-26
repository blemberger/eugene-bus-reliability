-- When did each bus actually reach each stop? Derived from VehiclePositions.
--
-- LTD's positions carry no stop fields, so the method is geometric: project each
-- position onto the trip's route shape to get a fraction 0..1 along the route,
-- take the running maximum over time (so GPS jitter can't move the bus backwards),
-- and whenever that fraction steps past a stop's fraction between two reports,
-- interpolate the crossing time linearly between them. Uncertainty is half the
-- gap between the two reports (±15 s at a 30 s poll while moving; larger if the
-- bus dwelt). A stop passed before the trip's first report has no lower bound
-- and is flagged is_bounded = false; marts exclude it. Positions more than 100 m
-- off the shape (detours, bad fixes) are ignored. An arrival further from the schedule
-- than the plausibility window (dbt_project.yml vars) is flagged is_plausible = false:
-- that is a bus reporting the wrong trip id, not a measurement of lateness.
--
-- The feed also reports, for stops already passed, a departure time that no
-- longer changes; int_feed_settled_times captures that as a second, independent
-- observation and fct_stop_events carries both so they can be compared.

{{ config(
    indexes=[
        {'columns': ['service_date', 'trip_id', 'stop_sequence'], 'unique': True},
        {'columns': ['observed_arrival']},
    ]
) }}

with positions as (
    select trip_id, service_date, vehicle_id, ts, frac, off_route_m
    from {{ ref('int_position_fractions') }}
),

stepped as (
    select *,
           max(frac) over (partition by trip_id, service_date order by ts
                           rows between unbounded preceding and current row) as frac_hi
    from positions
    where off_route_m <= 100
),

steps as (
    select trip_id, service_date, vehicle_id,
           ts as ts_hi, lag(ts) over w as ts_lo,
           frac_hi, lag(frac_hi) over w as frac_lo
    from stepped
    window w as (partition by trip_id, service_date order by ts)
),

crossings as (
    select * from steps where frac_hi > coalesce(frac_lo, -1)
),

sched as (
    select e.service_date, e.trip_id, e.stop_sequence, e.stop_id, e.route_id, e.direction_id,
           e.scheduled_arrival, e.is_timepoint, e.is_first_stop, e.is_last_stop, f.frac
    from {{ ref('int_scheduled_stop_events') }} e
    join {{ ref('int_stop_shape_fractions') }} f
      on f.feed_version_id = e.feed_version_id and f.trip_id = e.trip_id and f.stop_sequence = e.stop_sequence
    where f.is_usable
      and e.service_date between current_date - {{ var('lookback_days') }} and current_date
),

matched as (
    select
        s.service_date, s.trip_id, s.stop_sequence, s.stop_id, s.route_id, s.direction_id,
        s.scheduled_arrival, s.is_timepoint, s.is_first_stop, s.is_last_stop,
        c.vehicle_id,
        case when c.ts_lo is null then c.ts_hi
             else c.ts_lo + (c.ts_hi - c.ts_lo) * ((s.frac - c.frac_lo) / (c.frac_hi - c.frac_lo))
        end as observed_arrival,
        case when c.ts_lo is null then null
             else extract(epoch from c.ts_hi - c.ts_lo) / 2 end as uncertainty_s,
        c.ts_lo is not null as is_bounded
    from sched s
    join crossings c
      on c.trip_id = s.trip_id and c.service_date = s.service_date
     and s.frac > coalesce(c.frac_lo, -1) and s.frac <= c.frac_hi
)

select
    *,
    extract(epoch from observed_arrival - scheduled_arrival)
        between -{{ var('plausible_early_seconds') }} and {{ var('plausible_late_seconds') }} as is_plausible
from matched