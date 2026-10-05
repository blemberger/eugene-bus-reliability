-- How often LTD revises a stop's predicted arrival, over the last 7 days: one row, read by
-- the Accuracy page. Computed here because it reads every prediction row of
-- the week, which is too slow to run each time someone opens the page.
-- Recomputed at most hourly (macros/hourly.sql).

{{ config(**hourly_config()) }}

{{ hourly_start() }}
with recent as (
    select trip_id, start_date, stop_sequence, first_seen_at, last_seen_at
    from {{ source('rt', 'prediction_history') }}
    where start_date >= current_date - 7
    union all
    select trip_id, start_date, stop_sequence, first_seen_at, last_seen_at
    from {{ source('rt', 'prediction_current') }}
    where start_date >= current_date - 7
),

per_stop as (
    select count(*) as revisions,
           extract(epoch from max(last_seen_at) - min(first_seen_at)) as tracked_s
    from recent
    group by trip_id, start_date, stop_sequence
)

select now() as computed_at,
       count(*) as trip_stops,
       percentile_cont(0.5) within group (order by revisions) as median_revisions,
       percentile_cont(0.9) within group (order by revisions) as p90_revisions,
       percentile_cont(0.5) within group (order by tracked_s) as median_tracked_s
from per_stop
{{ hourly_end() }}