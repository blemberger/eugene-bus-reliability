-- Which schedule version applies to each service date.
--
-- Rule: for a date d, the feed version whose service coverage includes d and
-- which was downloaded most recently. Coverage is taken from calendar.txt and
-- calendar_dates.txt rather than feed_info.txt so it works when feed_info is
-- absent. Consequence worth knowing: a re-published feed downloaded mid-period
-- retroactively applies to earlier dates it covers. That is the intended
-- behaviour (a re-publish is a correction), and it is the reason realtime rows
-- carry a service date rather than a feed_version_id.

with coverage as (
    select feed_version_id, min(start_date) as cover_start, max(end_date) as cover_end
    from {{ source('gtfs', 'calendar') }}
    group by feed_version_id
    union all
    select feed_version_id, min(date), max(date)
    from {{ source('gtfs', 'calendar_dates') }}
    where exception_type = 1
    group by feed_version_id
),

span as (
    select
        c.feed_version_id,
        min(c.cover_start) as cover_start,
        max(c.cover_end)   as cover_end,
        max(v.downloaded_at) as downloaded_at
    from coverage c
    join {{ source('gtfs', 'feed_version') }} v using (feed_version_id)
    group by c.feed_version_id
),

-- Only dates that can have observations: from the day before collection began (a trip
-- running past midnight on the first day belongs to the previous service date) up to
-- tomorrow. Dates before collection, or months ahead, can never be matched to realtime
-- data and would only multiply every downstream table.
first_collected as (
    select coalesce(
        (select min(fetched_at at time zone '{{ var("timezone") }}')::date from {{ source('rt', 'fetch') }}),
        current_date
    ) - 1 as d
),

dates as (
    select d::date as service_date
    from generate_series(
        greatest((select min(cover_start) from span), (select d from first_collected)),
        least((select max(cover_end) from span), current_date + 1),
        interval '1 day'
    ) as d
)

select distinct on (dates.service_date)
    dates.service_date,
    span.feed_version_id
from dates
join span
  on dates.service_date between span.cover_start and span.cover_end
order by dates.service_date, span.downloaded_at desc