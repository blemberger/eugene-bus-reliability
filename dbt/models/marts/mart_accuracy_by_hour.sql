-- How often the sign was right, by route, minutes ahead and hour of day: the Accuracy page's
-- time-of-day chart, which adds these counts up over a range of minutes ahead. Rows with
-- route_id null are all routes together.

select
    case when grouping(route_id) = 1 then null else route_id end as route_id,
    horizon_min,
    hour_local,
    count(*)                                   as n,
    count(*) filter (where abs(error_s) <= 60)  as n_within_1min,
    count(*) filter (where abs(error_s) <= 120) as n_within_2min,
    count(*) filter (where abs(error_s) <= 180) as n_within_3min
from {{ ref('fct_prediction_errors') }}
where horizon_min between 0 and 30
group by grouping sets ((horizon_min, hour_local), (route_id, horizon_min, hour_local))