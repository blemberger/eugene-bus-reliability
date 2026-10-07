-- How often the countdown was right, by route, minutes ahead and hour of day: the Countdown
-- page's time-of-day chart, which adds these counts up over a range of minutes ahead. One count
-- per bus arrival per minute ahead (fct_countdown_samples), on the same arrivals as the rest of
-- that page. Rows with route_id null are all routes together.
-- Recomputed at most hourly (macros/hourly.sql).

{{ config(**hourly_config()) }}

{{ hourly_start() }}
select
    case when grouping(route_id) = 1 then null else route_id end as route_id,
    ahead_min as horizon_min,
    hour_local,
    count(*)                                   as n,
    count(*) filter (where abs(error_s) <= 60)  as n_within_1min,
    count(*) filter (where abs(error_s) <= 120) as n_within_2min,
    count(*) filter (where abs(error_s) <= 180) as n_within_3min
from {{ ref('fct_countdown_samples') }}
where in_comparison
group by grouping sets ((ahead_min, hour_local), (route_id, ahead_min, hour_local))
{{ hourly_end() }}