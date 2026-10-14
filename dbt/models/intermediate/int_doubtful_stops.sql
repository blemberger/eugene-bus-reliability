-- Stops (on one route, in one direction) where our GPS time can't be trusted: where it and
-- LTD's own record of when the bus left disagree by more than 2 minutes as a rule (the median
-- over the last 30 days, 20 or more arrivals timed both ways). Normally the two agree to within
-- seconds at timepoints and LTD's runs about a minute early between them (its own estimate
-- there); a gap twice that, held over weeks, is the stop sitting where our method places the
-- bus wrongly (a shape that doubles back, a stop far off the street the bus uses). Our GPS
-- times there are left out of every number (fct_stop_events), and so are the predictions
-- scored against them; the Data & methods page counts them.

{{ config(materialized='table') }}

with timed_both_ways as (
    select o.route_id, o.direction_id, o.stop_id,
           extract(epoch from f.settled_time - o.observed_arrival) as diff_s
    from {{ ref('int_observed_arrivals') }} o
    join {{ ref('int_feed_settled_times') }} f
      on f.trip_id = o.trip_id and f.service_date = o.service_date
     and f.stop_sequence = o.stop_sequence
    where o.is_bounded and f.is_settled and o.service_date >= current_date - 30
)

select route_id, direction_id, stop_id, count(*) as n,
       percentile_cont(0.5) within group (order by diff_s) as median_diff_s
from timed_both_ways
group by 1, 2, 3
having count(*) >= 20
   and abs(percentile_cont(0.5) within group (order by diff_s)) > 120