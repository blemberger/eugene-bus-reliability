-- (service_date, feed_version_id, service_id) for every day a service runs:
-- calendar.txt day-of-week pattern within [start_date, end_date], plus
-- calendar_dates.txt additions (exception_type 1), minus removals (2).

with fv as (
    select * from {{ ref('int_feed_version_by_date') }}
),

regular as (
    select
        fv.service_date,
        fv.feed_version_id,
        c.service_id
    from fv
    join {{ source('gtfs', 'calendar') }} c
      on c.feed_version_id = fv.feed_version_id
     and fv.service_date between c.start_date and c.end_date
    where case extract(isodow from fv.service_date)
              when 1 then c.monday
              when 2 then c.tuesday
              when 3 then c.wednesday
              when 4 then c.thursday
              when 5 then c.friday
              when 6 then c.saturday
              when 7 then c.sunday
          end
),

added as (
    select fv.service_date, fv.feed_version_id, cd.service_id
    from fv
    join {{ source('gtfs', 'calendar_dates') }} cd
      on cd.feed_version_id = fv.feed_version_id
     and cd.date = fv.service_date
    where cd.exception_type = 1
),

removed as (
    select fv.service_date, fv.feed_version_id, cd.service_id
    from fv
    join {{ source('gtfs', 'calendar_dates') }} cd
      on cd.feed_version_id = fv.feed_version_id
     and cd.date = fv.service_date
    where cd.exception_type = 2
)

select service_date, feed_version_id, service_id from regular
union
select service_date, feed_version_id, service_id from added
except
select service_date, feed_version_id, service_id from removed
