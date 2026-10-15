-- Stops (on one route, in one direction) where our GPS time can't be trusted: where, as a rule
-- (the median over the last 30 days, 20 or more trips), our time there is more than 2 minutes
-- out of line with our own times at the stops just before and after it on the same trip.
--
-- A bus's lateness changes smoothly from stop to stop, so a stop that sits minutes away from
-- both of its neighbours, trip after trip, is our method placing the bus wrongly there (a
-- route shape that doubles back, a stop mapped away from the street the bus uses), not how the
-- bus ran. The timetable's own guessed times between timepoints change smoothly too, so they
-- cancel out of this test. (An earlier version compared our time with LTD's; but between
-- timepoints LTD's time is its own estimate, so that flagged whole stretches where LTD's
-- estimate, not our time, was off.) Our GPS times at these stops are left out of every number
-- (fct_stop_events), and so are the predictions scored against them; the Data & methods page
-- counts them.

{{ config(materialized='table') }}

with ours as (
    select service_date, trip_id, stop_sequence, route_id, direction_id, stop_id,
           is_first_stop, is_last_stop,
           extract(epoch from observed_arrival - scheduled_arrival) as late_s
    from {{ ref('int_observed_arrivals') }}
    where is_bounded and is_plausible and service_date >= current_date - 30
),

around as (
    select *,
           lag(late_s) over w as before_s,
           lead(late_s) over w as after_s
    from ours
    window w as (partition by service_date, trip_id order by stop_sequence)
)

select route_id, direction_id, stop_id, count(*) as n,
       percentile_cont(0.5) within group (order by late_s - (before_s + after_s) / 2.0)
           as median_spike_s
from around
where before_s is not null and after_s is not null and not is_first_stop and not is_last_stop
group by 1, 2, 3
having count(*) >= 20
   and abs(percentile_cont(0.5) within group (order by late_s - (before_s + after_s) / 2.0)) > 120