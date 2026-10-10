-- Do the two independent observations of arrival agree? Position-derived
-- (int_observed_arrivals) vs the feed's settled departure time, per day and route.
--
-- Finding from the first two days: at timepoints the two agree to a median 31 s
-- (inside the ±15–20 s uncertainty of each), while at non-timepoints the feed's
-- time runs ~1 min earlier than the geometric crossing. LTD's system records
-- actual times at timepoints only; between them the "settled" value is the last
-- prediction, not an observation. So the geometric method is the reference for
-- every stop, and the feed is the cross-check at timepoints. Both sets of
-- columns are kept here so that claim stays checkable as data accumulates.
-- Built incrementally (macros/incremental.sql): each build recomputes the latest two service days.

{{ config(materialized='incremental', incremental_strategy='delete+insert', unique_key='service_date') }}


select
    service_date,
    route_id,
    route_short_name,
    count(*) filter (where observed_arrival is not null and is_bounded)   as n_position_method,
    count(*) filter (where feed_settled_time is not null)                  as n_feed_method,
    count(*) filter (where methods_diff_s is not null and is_bounded)      as n_both,
    percentile_cont(0.5) within group (order by methods_diff_s)            as median_diff_s,
    percentile_cont(0.5) within group (order by abs(methods_diff_s))       as median_abs_diff_s,
    percentile_cont(0.9) within group (order by abs(methods_diff_s))       as p90_abs_diff_s,
    count(*) filter (where abs(methods_diff_s) <= 60)                      as n_within_1min,
    count(*) filter (where abs(methods_diff_s) <= 120)                     as n_within_2min,
    -- timepoints only: the fair comparison
    count(*) filter (where methods_diff_s is not null and is_bounded and is_timepoint) as n_both_timepoints,
    percentile_cont(0.5) within group (order by methods_diff_s)
        filter (where is_timepoint)                                        as median_diff_timepoints_s,
    percentile_cont(0.5) within group (order by abs(methods_diff_s))
        filter (where is_timepoint)                                        as median_abs_diff_timepoints_s,
    count(*) filter (where abs(methods_diff_s) <= 60 and is_timepoint and is_bounded)
                                                                           as n_within_1min_timepoints
from {{ ref('fct_stop_events') }}
where trip_had_realtime and {{ recent_days() }}
group by 1, 2, 3