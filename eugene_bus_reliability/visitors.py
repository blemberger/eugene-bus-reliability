"""The site's visitor report, laid out like WordPress (Jetpack) stats so it compares with them:
views and visitors per day, week and month as bar charts, the totals, the pages people looked
at, where they came from, phones against computers, and the busiest day and hour.

Views and visitors are the site's own counts (site.page_view, written by the app): a view is a
page opened (or another stop or route chosen on it); a visitor is one browser on one day, as
Jetpack counts them (the visitor code, see sql/schema/040_site.sql), so per week and month the
visitors are the days' visitors added up. Visitor codes are recorded from the update that
added them (October 2026); earlier days show views only. The web server's access log (Caddy's, on stdin) adds the other
sites that sent people here and the plain report pages (/reports/), which the app doesn't see.
Bots and crawlers are left out of everything but their own line, and so is any browser that
opened the site with ?dont_count_me (the owner's).

`make visitors` prints the text version and copies the page with the charts to visitors.html.
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from html import escape
from pathlib import Path
from typing import TextIO
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import psycopg

LOCAL_TZ = ZoneInfo("America/Los_Angeles")
LOCAL = "viewed_at at time zone 'America/Los_Angeles'"
BOTS = re.compile(
    r"bot|crawl|spider|slurp|headless|lighthouse|preview|monitor|python|curl|wget|scan", re.I
)
# requests that are parts of a page rather than a page someone opened
NOT_A_PAGE = re.compile(r"^/(_stcore|static|media|component|favicon|robots|\.well-known)|\.\w+$")
PAGE_NAMES = {
    "overview": "Overview",
    "map": "Live map",
    "stops": "Stops",
    "routes": "Routes",
    "route": "Route report cards",
    "accuracy": "Predictions",
    "methods": "Data & methods",
    "arrivals": "Arrivals board",
    "status": "Status",
}
VIEWS_COLOUR = "#9cc0e0"  # the light blue of the site's 8-in-10 strips
VISITORS_COLOUR = "#1f5f9e"  # the site's blue
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


# ---- the numbers -------------------------------------------------------------------------------
def periods(cur: psycopg.Cursor, unit: str, count: int) -> list[dict]:
    """Views, visits and visitors per day / week (from Monday) / month, newest last, the last
    `count` of them (empty ones included). Visitors per week and month: the days' added up;
    None where no visitor codes were recorded yet."""
    rows = cur.execute(
        f"""
        with days as (
            select ({LOCAL})::date as day, count(*) as views,
                   count(distinct session_id) as visits,
                   count(distinct visitor) as visitors,
                   count(visitor) as coded,
                   count(*) filter (where device = 'phone') as phone_views
            from site.page_view where device <> 'bot'
            group by 1
        )
        select date_trunc(%s, day)::date as start, sum(views)::int as views,
               sum(visits)::int as visits,
               case when sum(coded) > 0 then sum(visitors)::int end as visitors,
               sum(phone_views)::int as phone_views
        from days group by 1 order by 1
        """,
        (unit,),
    ).fetchall()
    have = {r[0]: r for r in rows}
    today = datetime.now(tz=LOCAL_TZ).date()
    if unit == "day":
        starts = [today - timedelta(days=i) for i in range(count - 1, -1, -1)]
    elif unit == "week":
        monday = today - timedelta(days=today.weekday())
        starts = [monday - timedelta(weeks=i) for i in range(count - 1, -1, -1)]
    else:
        first = today.replace(day=1)
        starts = []
        for _ in range(count):
            starts.insert(0, first)
            first = (first - timedelta(days=1)).replace(day=1)
    out = []
    for s in starts:
        r = have.get(s)
        out.append(
            {
                "start": s,
                "views": r[1] if r else 0,
                "visits": r[2] if r else 0,
                "visitors": r[3] if r else None,
                "phone_views": r[4] if r else 0,
            }
        )
    return out


def totals(cur: psycopg.Cursor) -> dict:
    """Views and visitors today, yesterday, the last 7 and 30 days (and the 7 before), all
    time, and the best day ever."""
    since_day = f"({LOCAL})::date"
    today = "(now() at time zone 'America/Los_Angeles')::date"
    r = cur.execute(
        f"""
        with d as (
            select {since_day} as day, count(*) as views, count(distinct visitor) as visitors,
                   count(visitor) as coded
            from site.page_view where device <> 'bot' group by 1
        )
        select
            coalesce(sum(views) filter (where day = {today}), 0),
            sum(visitors) filter (where day = {today} and coded > 0),
            coalesce(sum(views) filter (where day = {today} - 1), 0),
            sum(visitors) filter (where day = {today} - 1 and coded > 0),
            coalesce(sum(views) filter (where day > {today} - 7), 0),
            sum(visitors) filter (where day > {today} - 7 and coded > 0),
            coalesce(sum(views) filter (where day <= {today} - 7 and day > {today} - 14), 0),
            sum(visitors) filter (where day <= {today} - 7 and day > {today} - 14 and coded > 0),
            coalesce(sum(views) filter (where day > {today} - 30), 0),
            sum(visitors) filter (where day > {today} - 30 and coded > 0),
            coalesce(sum(views), 0), sum(visitors) filter (where coded > 0), min(day)
        from d
        """
    ).fetchone()
    best = cur.execute(
        f"""
        select {since_day} as day, count(*) from site.page_view where device <> 'bot'
        group by 1 order by 2 desc, 1 desc limit 1
        """
    ).fetchone()
    keys = [
        "today",
        "today_v",
        "yesterday",
        "yesterday_v",
        "week",
        "week_v",
        "week_before",
        "week_before_v",
        "month",
        "month_v",
        "all",
        "all_v",
        "since",
    ]
    out = {
        k: (int(v) if v is not None and k != "since" else v) for k, v in zip(keys, r, strict=True)
    }
    out["best"] = best
    out["visitors_since"] = cur.execute(
        f"select min({since_day}) from site.page_view where visitor is not null"
    ).fetchone()[0]
    return out


def top_pages(cur: psycopg.Cursor, days: int) -> list[tuple[str, int, int]]:
    """(page, views, visitors) over the last `days` days, most viewed first; a stop's report
    card apart from the list of stops."""
    rows = cur.execute(
        """
        select case when page = 'stops' and detail is not null then 'stop card' else page end,
               count(*), case when count(visitor) > 0 then count(distinct visitor) end
        from site.page_view
        where device <> 'bot' and viewed_at > now() - make_interval(days => %s)
        group by 1 order by 2 desc
        """,
        (days,),
    ).fetchall()
    return [
        ("Stop report cards" if p == "stop card" else PAGE_NAMES.get(p, p), v, u)
        for p, v, u in rows
    ]


def looked_up(cur: psycopg.Cursor, days: int) -> list[tuple[str, int]]:
    """The routes and stops people opened, most first."""
    rows = cur.execute(
        """
        select v.page, v.detail, coalesce(s.stop_name, r.route_short_name, v.detail),
               r.route_long_name, count(*)
        from site.page_view v
        left join (select distinct on (stop_id) stop_id, stop_name from gtfs.stops
                   order by stop_id, feed_version_id desc) s
          on v.page = 'stops' and s.stop_id = v.detail
        left join (select distinct on (route_id) route_id, route_short_name, route_long_name
                   from gtfs.routes order by route_id, feed_version_id desc) r
          on v.page = 'route' and r.route_id = v.detail
        where v.detail is not null and v.device <> 'bot'
          and v.viewed_at > now() - make_interval(days => %s)
        group by 1, 2, 3, 4 order by 5 desc limit 15
        """,
        (days,),
    ).fetchall()
    return [
        ((f"Route {name}" + (f" ({long})" if long else "")) if page == "route" else f"Stop: {name}", n)
        for page, _, name, long, n in rows
    ]  # fmt: skip


def busiest(cur: psycopg.Cursor) -> tuple[list[int], list[int]]:
    """Views by day of the week (Monday first) and by hour of the day, all time."""
    dow = dict(
        cur.execute(
            f"select extract(isodow from {LOCAL})::int, count(*) from site.page_view "
            "where device <> 'bot' group by 1"
        ).fetchall()
    )
    hours = dict(
        cur.execute(
            f"select extract(hour from {LOCAL})::int, count(*) from site.page_view "
            "where device <> 'bot' group by 1"
        ).fetchall()
    )
    return [dow.get(d, 0) for d in range(1, 8)], [hours.get(h, 0) for h in range(24)]


def devices(cur: psycopg.Cursor, days: int) -> list[tuple[str, int, int]]:
    rows = cur.execute(
        """
        select device, count(*), case when count(visitor) > 0 then count(distinct visitor) end
        from site.page_view
        where device <> 'bot' and viewed_at > now() - make_interval(days => %s)
        group by 1 order by 2 desc
        """,
        (days,),
    ).fetchall()
    return [("Phones" if d == "phone" else "Computers", v, u) for d, v, u in rows]


def bots(cur: psycopg.Cursor, days: int) -> int:
    return cur.execute(
        "select count(*) from site.page_view where device = 'bot' "
        "and viewed_at > now() - make_interval(days => %s)",
        (days,),
    ).fetchone()[0]


def load_times(cur: psycopg.Cursor) -> list[tuple]:
    """How long pages took to draw, last 7 days, per page: views, median and slowest seconds."""
    return cur.execute(
        """
        select page, count(*),
               round((percentile_cont(0.5) within group (order by load_ms))::numeric / 1000, 1),
               round(max(load_ms) / 1000.0, 1)
        from site.page_view
        where device <> 'bot' and load_ms is not null and viewed_at > now() - interval '7 days'
        group by 1 order by 3 desc
        """
    ).fetchall()


def access_log(stream: TextIO, days: int = 30) -> dict:
    """From Caddy's access log: other sites that sent people here, the pages they landed on,
    and views of the plain report pages per day (people only)."""
    since = datetime.now(tz=UTC) - timedelta(days=days)
    sources: Counter[str] = Counter()
    landing: Counter[str] = Counter()
    reports: Counter[date] = Counter()
    report_pages: Counter[str] = Counter()
    for line in stream:
        try:
            entry = json.loads(line)
            request = entry["request"]
            when = datetime.fromtimestamp(float(entry["ts"]), tz=UTC)
        except (ValueError, KeyError, TypeError):
            continue
        path = urlsplit(request.get("uri", "")).path or "/"
        if (
            when < since
            or request.get("method") != "GET"
            # 304 = "not changed": a returning visitor whose browser already had the page
            or entry.get("status") not in (200, 304)
            or NOT_A_PAGE.search(path)
        ):
            continue
        headers = request.get("headers") or {}
        ua = (headers.get("User-Agent") or [""])[0]
        if not ua or BOTS.search(ua):
            continue
        if path.startswith("/reports"):
            reports[when.astimezone(LOCAL_TZ).date()] += 1
            report_pages[path] += 1
        landing[path] += 1
        referrer = urlsplit((headers.get("Referer") or [""])[0]).hostname or ""
        own = (request.get("host") or "").removeprefix("www.")
        if referrer and referrer.removeprefix("www.") != own:
            sources[referrer.removeprefix("www.")] += 1
    return {
        "sources": sources,
        "landing": landing,
        "reports": reports,
        "report_pages": report_pages,
    }


# ---- the text version ----------------------------------------------------------------------------
def label(start: date, unit: str) -> str:
    if unit == "day":
        return start.strftime("%a %b %d")
    if unit == "week":
        return "wk of " + start.strftime("%b %d")
    return start.strftime("%b %Y")


def text_bars(rows: list[dict], unit: str, width: int = 40) -> list[str]:
    """One line per period: a bar as long as the views, its first part (█) the visitors and
    the rest (░) the other views, then the numbers."""
    top = max((r["views"] for r in rows), default=0) or 1
    lines = [f"{'':<13} {'':<{width}}  {'views':>6}  {'visitors':>8}"]
    for r in rows:
        views = r["views"]
        visitors = r["visitors"]
        full = round(views / top * width)
        dark = min(full, round((visitors or 0) / top * width))
        bar = "█" * dark + "░" * (full - dark)
        who = "–" if visitors is None else f"{visitors:,}"
        lines.append(f"{label(r['start'], unit):<13} {bar:<{width}}  {views:>6,}  {who:>8}")
    return lines


def change(now: int, before: int) -> str:
    if not before:
        return ""
    pct = round(100 * (now - before) / before)
    return f" ({'up' if pct >= 0 else 'down'} {abs(pct)}% on the 7 days before)"


def text_report(d: dict) -> str:
    t = d["totals"]
    out = ["EUGENE BUS WATCH — visitors (laid out like WordPress stats)", ""]

    def vis(v):
        return "–" if v is None else f"{v:,}"

    out += [
        "== AT A GLANCE",
        f"Today:          {t['today']:,} views, {vis(t['today_v'])} visitors",
        f"Yesterday:      {t['yesterday']:,} views, {vis(t['yesterday_v'])} visitors",
        f"Last 7 days:    {t['week']:,} views, {vis(t['week_v'])} visitors"
        + change(t["week"], t["week_before"]),
        f"Last 30 days:   {t['month']:,} views, {vis(t['month_v'])} visitors",
        f"All time:       {t['all']:,} views since {t['since']}"
        + (f"; best day {t['best'][0]} with {t['best'][1]:,} views" if t["best"] else ""),
        "(visitors: one browser on one day, as WordPress counts them; per week and month the "
        "days' visitors added up; "
        + (
            f"counted since {t['visitors_since']}, so earlier days show –)"
            if t["visitors_since"]
            else "not counted yet: they start with the next page view)"
        ),
        "",
    ]
    for unit, title in (("day", "per day, last 30 days"), ("week", "per week, last 12 weeks"),
                        ("month", "per month")):  # fmt: skip
        out += [f"== Views and visitors {title}    █ visitors  ░ other views"]
        out += text_bars(d[unit], unit) + [""]
    out += ["== Pages, last 30 days                 views  visitors"]
    out += [f"{p:<34} {v:>6}  {'–' if u is None else u:>8}" for p, v, u in d["pages"]] or [
        "(none yet)"
    ]
    out += ["", "== Routes and stops people opened, last 30 days"]
    out += [f"{n:6}  {name}" for name, n in d["looked_up"]] or ["(none yet)"]
    log = d["log"]
    if log is not None:
        out += ["", "== Referrers: other sites that sent people here, last 30 days"]
        out += [f"{n:6}  {s}" for s, n in log["sources"].most_common(15)] or [
            "(none yet: everyone typed the address, used a bookmark, or their browser hid it)"
        ]
        out += ["", "== Plain report pages (/reports/) opened by people, last 30 days"]
        out += [f"{n:6}  {p}" for p, n in log["report_pages"].most_common(10)] or ["(none yet)"]
    out += ["", "== Phones and computers, last 30 days    views  visitors"]
    out += [f"{name:<34} {v:>6}  {'–' if u is None else u:>8}" for name, v, u in d["devices"]]
    dow, hours = d["busiest"]
    if sum(dow):
        best_day = max(range(7), key=lambda i: dow[i])
        best_hour = max(range(24), key=lambda h: hours[h])
        out += [
            "",
            "== Busiest day and hour (all time)",
            f"Most popular day:  {WEEKDAYS[best_day]} ({round(100 * dow[best_day] / sum(dow))}% "
            "of views)",
            f"Most popular hour: {hour_label(best_hour)} ({round(100 * hours[best_hour] / sum(hours))}"
            "% of views)",
        ]  # fmt: skip
    out += ["", f"(bots and crawlers left out: {d['bots']:,} views in the last 30 days)"]
    if d["load"]:
        out += ["", "== How long pages took to draw, last 7 days   views  median_s  slowest_s"]
        out += [f"{PAGE_NAMES.get(p, p):<34} {n:>6}  {m:>8}  {s:>9}" for p, n, m, s in d["load"]]
    return "\n".join(out)


def hour_label(h: int) -> str:
    return f"{h % 12 or 12} {'am' if h < 12 else 'pm'}"


# ---- the page with the charts --------------------------------------------------------------------
def svg_bars(rows: list[dict], unit: str) -> str:
    """Bars like WordPress stats': each period's views (light), its visitors (dark) inside."""
    w, h, left, bottom, top_pad = 900, 260, 44, 40, 14
    n = max(1, len(rows))
    slot = (w - left - 8) / n
    bar = min(48.0, max(4.0, slot * 0.7))
    top = max((r["views"] for r in rows), default=0)
    nice = next((s for s in (5, 10, 20, 25, 50, 100, 200, 250, 500, 1000, 2000, 5000)
                 if top <= s * 5), 10000)  # fmt: skip
    ymax = max(nice * 5, 5)

    def y(v: float) -> float:
        return h - bottom - (h - bottom - top_pad) * v / ymax

    parts = [f"<svg viewBox='0 0 {w} {h}' role='img' aria-label='views and visitors per {unit}'>"]
    for i in range(6):
        v = ymax * i / 5
        parts.append(
            f"<line x1='{left}' x2='{w}' y1='{y(v):.1f}' y2='{y(v):.1f}' class='grid'/>"
            f"<text x='{left - 6}' y='{y(v) + 4:.1f}' class='tick' text-anchor='end'>{v:g}</text>"
        )
    every = max(1, -(-n // 8))  # at most about 8 labels
    for i, r in enumerate(rows):
        x = left + i * slot + (slot - bar) / 2
        views, visitors = r["views"], r["visitors"]
        tip = f"{label(r['start'], unit)}: {views:,} views" + (
            f", {visitors:,} visitors" if visitors is not None else ""
        )
        # a hit target the whole height of the slot, so a tiny bar still shows its numbers
        parts.append(
            f"<g><title>{escape(tip)}</title>"
            f"<rect x='{left + i * slot:.1f}' y='{top_pad}' width='{slot:.1f}' "
            f"height='{h - bottom - top_pad}' fill='transparent'/>"
        )
        if views:
            parts.append(
                f"<rect x='{x:.1f}' y='{y(views):.1f}' width='{bar:.1f}' "
                f"height='{y(0) - y(views):.1f}' rx='3' fill='{VIEWS_COLOUR}'/>"
            )
        if visitors:
            inner = bar * 0.55
            parts.append(
                f"<rect x='{x + (bar - inner) / 2:.1f}' y='{y(visitors):.1f}' "
                f"width='{inner:.1f}' height='{y(0) - y(visitors):.1f}' rx='2' "
                f"fill='{VISITORS_COLOUR}'/>"
            )
        parts.append("</g>")
        if i % every == 0 or i == n - 1:
            parts.append(
                f"<text x='{left + i * slot + slot / 2:.1f}' y='{h - bottom + 18}' class='tick' "
                f"text-anchor='middle'>{escape(axis_label(r['start'], unit))}"
                "</text>"
            )
    parts.append("</svg>")
    return "".join(parts)


def axis_label(start: date, unit: str) -> str:
    return start.strftime("%b %Y") if unit == "month" else f"{start:%b} {start.day}"


def html_table(head: list[str], rows: list[list]) -> str:
    if not rows:
        return "<p class='muted'>None yet.</p>"
    return (
        "<table><tr>"
        + "".join(f"<th>{escape(h)}</th>" for h in head)
        + "</tr>"
        + "".join("<tr>" + "".join(f"<td>{escape(str(c))}</td>" for c in r) + "</tr>" for r in rows)
        + "</table>"
    )


def html_report(d: dict) -> str:
    t = d["totals"]

    def vis(v):
        return "–" if v is None else f"{v:,}"

    tiles = "".join(
        f"<div class='tile'><div class='k'>{escape(k)}</div><div class='v'>{v:,}</div>"
        f"<div class='s'>views · {vis(u)} visitors</div></div>"
        for k, v, u in (
            ("Today", t["today"], t["today_v"]),
            ("Yesterday", t["yesterday"], t["yesterday_v"]),
            ("Last 7 days", t["week"], t["week_v"]),
            ("Last 30 days", t["month"], t["month_v"]),
            ("All time", t["all"], t["all_v"]),
        )
    )
    charts = "".join(
        f"<section><h2>{title}</h2>{svg_bars(d[unit], unit)}</section>"
        for unit, title in (
            ("day", "Views and visitors per day"),
            ("week", "Per week"),
            ("month", "Per month"),
        )
    )
    log = d["log"]
    dow, hours = d["busiest"]
    insight = ""
    if sum(dow):
        bd = max(range(7), key=lambda i: dow[i])
        bh = max(range(24), key=lambda i: hours[i])
        insight = (
            f"<p>Most popular day: <b>{WEEKDAYS[bd]}</b> ({round(100 * dow[bd] / sum(dow))}% of "
            f"views). Most popular hour: <b>{hour_label(bh)}</b> "
            f"({round(100 * hours[bh] / sum(hours))}% of views)."
            + (
                f" Best day ever: <b>{t['best'][0]:%a %b %-d}</b>, {t['best'][1]:,} views."
                if t["best"]
                else ""
            )
            + "</p>"
        )
    lists = (
        "<div class='cols'><section><h2>Pages, last 30 days</h2>"
        + html_table(
            ["Page", "Views", "Visitors"],
            [[p, v, "–" if u is None else u] for p, v, u in d["pages"]],
        )
        + "</section><section><h2>Routes and stops opened, last 30 days</h2>"
        + html_table(["Route or stop", "Views"], [list(r) for r in d["looked_up"]])
        + "</section>"
    )
    if log is not None:
        lists += (
            "<section><h2>Referrers, last 30 days</h2>"
            + html_table(["Site", "Visits sent"], [[s, n] for s, n in log["sources"].most_common(15)])
            + "</section><section><h2>Plain report pages opened, last 30 days</h2>"
            + html_table(["Page", "Views"], [[p, n] for p, n in log["report_pages"].most_common(10)])
            + "</section>"
        )  # fmt: skip
    lists += (
        "<section><h2>Phones and computers, last 30 days</h2>"
        + html_table(
            ["Device", "Views", "Visitors"],
            [[n, v, "–" if u is None else u] for n, v, u in d["devices"]],
        )
        + "</section><section><h2>Busiest day and hour</h2>"
        + insight
        + "</section></div>"
    )
    made = datetime.now(tz=LOCAL_TZ).strftime("%b %-d, %Y %-I:%M %p")
    since_txt = (
        f"Visitors are counted from {t['visitors_since']:%b %-d, %Y}; earlier bars show views only."
        if t["visitors_since"]
        else "Visitors aren't counted yet: they start with the next page view."
    )
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Eugene Bus Watch visitors</title>
<style>
body{{font-family:-apple-system,"Segoe UI",Roboto,sans-serif;background:#f3f5f7;color:#262730;
margin:0;padding:24px}}
main{{max-width:980px;margin:0 auto}}
h1{{font-size:1.5rem;margin:0 0 4px}} h2{{font-size:1.05rem;margin:0 0 10px}}
section{{background:#fff;border-radius:8px;box-shadow:0 1px 3px rgba(20,30,45,.08);
padding:16px 18px;margin:14px 0}}
.tiles{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}}
.tile{{background:#fff;border-radius:8px;box-shadow:0 1px 3px rgba(20,30,45,.08);padding:12px 14px}}
.k{{font-size:.85rem;color:#5f6368}} .v{{font-size:1.6rem;font-weight:700}}
.s{{font-size:.85rem;color:#5f6368}}
.key i{{display:inline-block;width:12px;height:12px;border-radius:2px;margin:0 5px 0 12px;
vertical-align:-1px}}
svg{{width:100%;height:auto}} .grid{{stroke:#e9ecee}} .tick{{font-size:12px;fill:#5f6368}}
table{{border-collapse:collapse;width:100%;font-size:.92rem}}
th,td{{text-align:left;padding:5px 6px;border-bottom:1px solid #eef0f2}}
th{{color:#5f6368;font-weight:600}} td:nth-child(n+2),th:nth-child(n+2){{text-align:right}}
.cols{{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:0 14px}}
.muted{{color:#5f6368}}
</style></head><body><main>
<h1>Eugene Bus Watch visitors</h1>
<p class="muted">Made {escape(made)}. Laid out like WordPress stats. A view is a page opened; a
visitor is one browser on one day, as WordPress counts them (per week and month, the days'
visitors added up). {escape(since_txt)} Bots and your own browsers (opened once with
?dont_count_me) are left out.</p>
<div class="tiles">{tiles}</div>
<p class="key"><i style="background:{VIEWS_COLOUR}"></i>Views
<i style="background:{VISITORS_COLOUR}"></i>Visitors
<span class="muted">· hover a bar for its numbers</span></p>
{charts}
{lists}
</main></body></html>
"""


# ---- putting it together ---------------------------------------------------------------------------
def gather(database_url: str, log_stream: TextIO | None) -> dict:
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        d = {
            "day": periods(cur, "day", 30),
            "week": periods(cur, "week", 12),
            "month": periods(cur, "month", 12),
            "totals": totals(cur),
            "pages": top_pages(cur, 30),
            "looked_up": looked_up(cur, 30),
            "devices": devices(cur, 30),
            "busiest": busiest(cur),
            "bots": bots(cur, 30),
            "load": load_times(cur),
        }
    # the periods before the site's first view are left off
    for unit in ("day", "week", "month"):
        first = next((i for i, r in enumerate(d[unit]) if r["views"]), len(d[unit]) - 1)
        d[unit] = d[unit][first:]
    d["log"] = access_log(log_stream) if log_stream is not None else None
    return d


def main(database_url: str, html_path: str | None, read_log: bool) -> None:
    d = gather(database_url, sys.stdin if read_log else None)
    print(text_report(d))
    if html_path:
        Path(html_path).write_text(html_report(d))
