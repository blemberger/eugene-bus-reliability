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
\echo '== How long pages took to draw, last 7 days (on the server: mostly waiting on the database)'
\echo '   during_build: whether an analysis build was running at the time'
select coalesce(b.during, false) as during_build, count(*) as page_views,
       round((percentile_cont(0.5) within group (order by v.load_ms))::numeric / 1000, 1) as median_s,
       round((percentile_cont(0.9) within group (order by v.load_ms))::numeric / 1000, 1) as p90_s,
       round(100.0 * avg((v.load_ms > 3000)::int)) as pct_over_3s
from site.page_view v
left join lateral (
    select true as during from analytics.build_log l
    where v.viewed_at between l.started_at and coalesce(l.finished_at, now()) limit 1
) b on true
where v.device <> 'bot' and v.load_ms is not null and v.viewed_at > now() - interval '7 days'
group by 1 order by 1;

\echo '== The slowest pages to draw, last 7 days'
select page, count(*) as page_views,
       round((percentile_cont(0.5) within group (order by load_ms))::numeric / 1000, 1) as median_s,
       round(max(load_ms) / 1000.0, 1) as slowest_s
from site.page_view
where device <> 'bot' and load_ms is not null and viewed_at > now() - interval '7 days'
group by 1 order by 3 desc;

-- The bottom line: one sentence to quote, and the numbers behind it. A visit is one browser
-- tab's session (people aren't tracked from one visit to the next, so this counts visits, not
-- distinct people). Browsers that opened the site with ?dont_count_me (yours) aren't counted.
\echo ''
\echo '== AT A GLANCE'
\x on
with v as (
    select session_id, viewed_at, device from site.page_view where device <> 'bot'
),
n as (
    select
        count(distinct session_id) filter (where viewed_at > now() - interval '7 days') as v7,
        count(*) filter (where viewed_at > now() - interval '7 days') as p7,
        count(distinct session_id) filter (where viewed_at <= now() - interval '7 days'
                                             and viewed_at > now() - interval '14 days') as v7_before,
        count(distinct session_id) filter (where viewed_at > now() - interval '30 days') as v30,
        count(*) filter (where viewed_at > now() - interval '30 days') as p30,
        count(distinct session_id) filter (where viewed_at > now() - interval '7 days'
                                             and device = 'phone') as phone7,
        count(distinct session_id) as v_all,
        min(viewed_at) as since
    from v
)
select format(
    'About %s visits a day over the last 7 days (%s page views a day)%s; %s visits in the last 30 days, %s%% of last week''s on phones.',
    round(v7 / 7.0, 1),
    round(p7 / 7.0, 1),
    case when v7_before > 0
         then format(', %s%s%% on the week before',
                     case when v7 >= v7_before then 'up ' else 'down ' end,
                     abs(round(100.0 * (v7 - v7_before) / v7_before)))
         else '' end,
    v30,
    coalesce(round(100.0 * phone7 / nullif(v7, 0)), 0)
) as summary,
       round(v7 / 7.0, 1) as visits_per_day_7d, round(p7 / 7.0, 1) as page_views_per_day_7d,
       v7 as visits_7d, v7_before as visits_week_before, v30 as visits_30d,
       p30 as page_views_30d, v_all as visits_ever,
       (since at time zone 'America/Los_Angeles')::date as counting_since
from n;
\x off