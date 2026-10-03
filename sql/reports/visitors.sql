-- Who visits the site: `make visitors` (from the laptop) or `make server-visitors` (on the
-- server). Reads site.page_view as the database owner. Bots are left out of every count
-- except the last. A visit = one browser tab's session; a page view = opening a page (or
-- choosing another stop or route on it). Times are Eugene time.
\pset footer off
\echo '== Visits per day, last 30 days'
select (viewed_at at time zone 'America/Los_Angeles')::date as day,
       count(distinct session_id) as visits,
       count(*) as page_views,
       round(100.0 * count(distinct session_id) filter (where device = 'phone')
             / nullif(count(distinct session_id), 0)) as pct_on_phones
from site.page_view
where device <> 'bot' and viewed_at > now() - interval '30 days'
group by 1 order by 1 desc;

\echo '== Pages, last 7 days'
select page, count(*) as page_views, count(distinct session_id) as visits
from site.page_view
where device <> 'bot' and viewed_at > now() - interval '7 days'
group by 1 order by 2 desc;

\echo '== Stops and routes people looked up, last 30 days'
select v.page, v.detail as id, coalesce(s.stop_name, r.route_long_name, '') as name,
       count(*) as page_views
from site.page_view v
left join (select distinct on (stop_id) stop_id, stop_name from gtfs.stops
           order by stop_id, feed_version_id desc) s on v.page = 'stops' and s.stop_id = v.detail
left join (select distinct on (route_id) route_id, route_long_name from gtfs.routes
           order by route_id, feed_version_id desc) r on v.page = 'route' and r.route_id = v.detail
where v.detail is not null and v.device <> 'bot' and v.viewed_at > now() - interval '30 days'
group by 1, 2, 3 order by 4 desc limit 15;

\echo '== Busiest hours of the day, last 30 days'
select extract(hour from viewed_at at time zone 'America/Los_Angeles')::int as hour,
       count(*) as page_views
from site.page_view
where device <> 'bot' and viewed_at > now() - interval '30 days'
group by 1 order by 1;

\echo '== Bots and crawlers that ran the site, last 7 days (search engines, link previews)'
select count(*) as page_views, count(distinct session_id) as sessions
from site.page_view
where device = 'bot' and viewed_at > now() - interval '7 days';