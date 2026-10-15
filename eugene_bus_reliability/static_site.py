"""Plain HTML report pages for every route and stop, plus a sitemap and robots.txt.

The site itself is drawn by JavaScript after the page loads (Streamlit), so a search engine or
an AI answer engine reading the HTML finds almost nothing there. These pages say the same
headline numbers in plain HTML: one page per route (/reports/route/11/), one per stop with
enough measured arrivals (/reports/stop/01185/), and an index of them all (/reports/), each
with a button into the interactive report card. Caddy serves them as files (Caddyfile).

The scheduler writes them after an analysis build, at most every REBUILD_HOURS; `python -m
eugene_bus_reliability static-site` writes them by hand. Numbers: the last 30 days, all days.
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime
from html import escape
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import psycopg
from psycopg.rows import dict_row

from eugene_bus_reliability.names import clean_stop_name

log = logging.getLogger(__name__)

LOCAL_TZ = ZoneInfo("America/Los_Angeles")
REBUILD_HOURS = 6
MIN_ARRIVALS = 20  # a stop gets a page once this many arrivals were measured in 30 days
BRAND = "#1f5f9e"
GO = "#1d6b48"
INK = "#1b2533"


def out_dir() -> Path:
    return Path(os.environ.get("STATIC_SITE_DIR", "/site"))


def base_url() -> str | None:
    """https://<the site's name>, from SITE_ADDRESS (the server's .env, as Caddy uses it);
    None while the site is served on a bare IP (":80")."""
    names = os.environ.get("SITE_ADDRESS", "").split()
    name = names[0] if names else ""
    if not name or name.startswith(":") or "." not in name:
        return None
    return "https://" + name.removeprefix("https://").removeprefix("http://").rstrip("/")


# ---- words ------------------------------------------------------------------------------------


def delay_words(seconds) -> str:
    """'1.6 min late', '0.4 min early', 'on time' (as the site writes it)."""
    if seconds is None:
        return "—"
    s = float(seconds)
    m = abs(s) / 60
    if round(m, 1) == 0:
        return "on time"
    txt = f"{m:.1f}" if m < 9.95 else f"{m:.0f}"
    return f"{txt} min {'late' if s > 0 else 'early'}"


def end_words(seconds) -> str:
    m = round(float(seconds) / 60)
    return "on time" if m == 0 else f"{abs(m)} min {'late' if m > 0 else 'early'}"


def range_words(p10, p90) -> str:
    """'between on time and 4 min late': where 8 in 10 buses fell."""
    if p10 is None or p90 is None:
        return "—"
    return f"between {end_words(p10)} and {end_words(p90)}"


def range_short(p10, p90) -> str:
    """'−1 to 4 min', as the site's tables write it."""
    if p10 is None or p90 is None:
        return "—"
    a, b = round(float(p10) / 60), round(float(p90) / 60)
    return f"{a:d} to {b:d} min".replace("-", "−")


def off_words(seconds) -> str:
    return "—" if seconds is None else f"{float(seconds) / 60:.1f} min"


def hour_label(h: int) -> str:
    h = int(h) % 24
    return f"{h % 12 or 12} {'am' if h < 12 else 'pm'}"


def service_hour_key(h) -> int:
    return (int(h) - 4) % 24


# ---- the chart: the typical bus by hour, with the 8-in-10 band, as inline SVG ----------------


def hour_chart(rows: list[dict]) -> str:
    """hour_svg() twice: wide for a computer, narrow (with fewer hour labels) for a phone, so
    its text stays readable at either size; CSS shows one."""
    wide = hour_svg(rows, 720, 280, 9, "chart wide")
    if not wide:
        return ""
    return (
        wide
        + hour_svg(rows, 380, 260, 4, "chart narrow")
        + "<p class='key'><span><i class='k-line'></i>Typical bus</span>"
        "<span><i class='k-band'></i>8 in 10 buses</span><span><i class='k-dash'></i>On time</span></p>"
    )


def hour_svg(rows: list[dict], w: int, h: int, n_labels: int, cls: str) -> str:
    """The typical bus by the hour it was scheduled (line) and the band 8 in 10 buses fell in,
    against the timetable (dashed line at on time), as on the site. rows: hour_local,
    median_delay_s, p10_delay_s, p90_delay_s, n; hours with fewer than 10 arrivals left out."""
    rows = sorted(
        (r for r in rows if r["n"] >= 10 and r["p10_delay_s"] is not None),
        key=lambda r: service_hour_key(r["hour_local"]),
    )
    if len(rows) < 3:
        return ""
    left, right, top, bottom = 64, 12, 14, 34
    lo = min(0.0, min(float(r["p10_delay_s"]) / 60 for r in rows)) - 0.5
    hi = max(1.0, max(float(r["p90_delay_s"]) / 60 for r in rows)) + 0.5
    step = (w - left - right) / max(1, len(rows) - 1)

    def x(i: int) -> float:
        return left + i * step

    def y(minutes: float) -> float:
        return top + (hi - minutes) / (hi - lo) * (h - top - bottom)

    band = [(x(i), y(float(r["p90_delay_s"]) / 60)) for i, r in enumerate(rows)] + [
        (x(i), y(float(r["p10_delay_s"]) / 60)) for i, r in reversed(list(enumerate(rows)))
    ]
    line = [(x(i), y(float(r["median_delay_s"]) / 60)) for i, r in enumerate(rows)]
    pts = " ".join(f"{a:.1f},{b:.1f}" for a, b in band)
    path = " ".join(f"{a:.1f},{b:.1f}" for a, b in line)
    span = hi - lo
    tick = 1 if span <= 7 else 2 if span <= 14 else 5
    ticks = range(int(lo // tick * tick) + tick, int(hi) + 1, tick)
    grid = "".join(
        f"<line x1='{left}' x2='{w - right}' y1='{y(t):.1f}' y2='{y(t):.1f}' "
        + (
            "stroke='#4b5563' stroke-width='1.6' stroke-dasharray='6 5'/>"
            if t == 0
            else "stroke='#e3e6ea'/>"
        )
        + f"<text x='{left - 6}' y='{y(t) + 4:.1f}' text-anchor='end'>"
        + ("on time" if t == 0 else f"{abs(t)} min {'late' if t > 0 else 'early'}")
        + "</text>"
        for t in ticks
    )
    every = max(1, round(len(rows) / n_labels))
    labels = "".join(
        f"<text x='{x(i):.1f}' y='{h - 12}' text-anchor='middle'>{hour_label(r['hour_local'])}</text>"
        for i, r in enumerate(rows)
        if i % every == 0
    )
    dots = "".join(f"<circle cx='{a:.1f}' cy='{b:.1f}' r='3.2' fill='{BRAND}'/>" for a, b in line)
    return (
        f"<svg viewBox='0 0 {w} {h}' role='img' aria-label='The typical bus by hour of the day, "
        f"with the range 8 in 10 buses fell in' class='{cls}'>"
        f"<g font-size='12' fill='#5f6368'>{grid}{labels}</g>"
        f"<polygon points='{pts}' fill='{BRAND}' fill-opacity='0.18'/>"
        f"<polyline points='{path}' fill='none' stroke='{BRAND}' stroke-width='3'/>{dots}</svg>"
    )


# ---- pages ---------------------------------------------------------------------------------

CSS = f"""
*{{box-sizing:border-box}}
body{{margin:0;background:#f4f6f8;color:#262730;font:16px/1.5 'Source Sans 3','Source Sans Pro',
-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif}}
header{{background:#fff;border-bottom:1px solid #e3e6ea}}
header a{{display:inline-flex;align-items:center;gap:10px;padding:12px 16px;color:{INK};
text-decoration:none;font-weight:700;font-size:1.25rem}}
main{{max-width:60rem;margin:0 auto;padding:20px 16px 40px}}
h1{{font-size:2rem;line-height:1.2;margin:.6rem 0 .4rem;color:{INK}}}
h2{{font-size:1.3rem;margin:0 0 .6rem;color:{INK}}}
.card{{background:#fff;border:1px solid #e3e6ea;border-radius:12px;padding:18px 20px;margin:16px 0;
box-shadow:0 1px 3px rgba(20,30,45,.08)}}
.lede{{font-size:1.08rem}}
.go{{display:inline-block;border:2px solid {GO};color:{GO};border-radius:8px;padding:8px 16px;
font-weight:650;text-decoration:none;background:#fff}}
.go:hover{{background:#e8f3ee}}
.nums{{display:grid;grid-template-columns:repeat(auto-fit,minmax(8.5rem,1fr));gap:12px;margin:12px 0}}
.nums div{{font-size:.9rem;color:#5f6368}} .nums b{{display:block;font-size:1.6rem;color:{INK};
font-weight:600}}
table{{width:100%;border-collapse:collapse;font-size:.95rem}}
th,td{{padding:7px 8px;border-bottom:1px solid #eef0f2;text-align:center}}
th{{background:#f6f8f9;font-size:.85rem;color:#555}}
th:first-child,td:first-child{{text-align:left}}
td a{{color:{GO};font-weight:600;text-decoration:none}}
.chart{{width:100%;height:auto}} .narrow{{display:none}}
@media (max-width:600px){{.wide{{display:none}} .narrow{{display:block}}}}
.key{{font-size:.9rem;color:#5f6368;margin:.2rem 0 0}}
.key span{{white-space:nowrap;margin-right:14px}}
.key i{{display:inline-block;width:22px;height:10px;margin-right:6px;vertical-align:middle}}
.key .k-line{{height:3px;background:{BRAND}}}
.key .k-band{{background:{BRAND};opacity:.18}}
.key .k-dash{{height:0;border-top:2px dashed #4b5563}}
.small{{font-size:.9rem;color:#5f6368}}
.cols{{columns:16rem;column-gap:24px}} .cols a{{display:block;color:{GO};text-decoration:none;
padding:2px 0}}
footer{{max-width:60rem;margin:0 auto;padding:0 16px 40px;color:#5f6368;font-size:.9rem}}
footer a{{color:{BRAND}}}
"""

LOGO = (
    "<svg width='30' height='30' viewBox='0 0 56 56' aria-hidden='true'>"
    f"<rect width='56' height='56' rx='13' fill='{BRAND}'/>"
    "<rect x='13' y='9' width='30' height='33' rx='6' fill='#fff'/>"
    f"<rect x='17' y='14' width='22' height='12' rx='2.5' fill='{BRAND}'/>"
    f"<circle cx='20' cy='34' r='2.6' fill='{BRAND}'/><circle cx='36' cy='34' r='2.6' "
    f"fill='{BRAND}'/><rect x='16' y='42' width='6' height='5' rx='1.5' fill='#fff'/>"
    "<rect x='34' y='42' width='6' height='5' rx='1.5' fill='#fff'/></svg>"
)


def page(title: str, description: str, path: str, body: str, base: str | None, updated: str) -> str:
    canonical = f"{base}{path}" if base else path
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title>
<meta name="description" content="{escape(description)}">
<link rel="canonical" href="{escape(canonical)}">
<meta property="og:title" content="{escape(title)}">
<meta property="og:description" content="{escape(description)}">
<meta property="og:type" content="website"><meta property="og:site_name" content="Eugene Bus Watch">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Source+Sans+3:wght@400;600;700&display=swap">
<style>{CSS}</style></head>
<body><header><a href="/">{LOGO}Eugene Bus Watch</a></header>
<main>{body}</main>
<footer><p>Last 30 days, all days, updated {escape(updated)}. Eugene Bus Watch measures when Lane
Transit District's buses actually reach each stop, from LTD's public schedule and live bus
positions; independent, not affiliated with LTD. <a href="/methods">How it's measured</a> ·
<a href="/reports/">Every route and stop</a> · <a href="/">The interactive site</a></p></footer>
</body></html>
"""


def numbers(r: dict, what: str) -> str:
    return (
        "<div class='nums'>"
        f"<div>Typical bus{escape(what)}<b>{delay_words(r['median_delay_s'])}</b></div>"
        f"<div>8 in 10 buses<b>{range_short(r['p10_delay_s'], r['p90_delay_s'])}</b></div>"
        f"<div>Minutes off, on average<b>{off_words(r['mean_abs_delay_s'])}</b></div>"
        f"<div>Arrivals measured<b>{int(r['n']):,}</b></div></div>"
    )


def prediction_sentence(p: dict | None) -> str:
    if not p or p.get("pred_s") is None or p.get("tt_s") is None:
        return ""
    return (
        " When LTD's real-time prediction (what apps like Transit show) said the bus was 5 "
        f"minutes away, it was {off_words(p['pred_s'])} off on average, against "
        f"{off_words(p['tt_s'])} for the printed timetable on the same buses."
    )


def table(head: list[str], rows: list[list[str]]) -> str:
    return (
        "<table><thead><tr>"
        + "".join(f"<th>{escape(h)}</th>" for h in head)
        + "</tr></thead><tbody>"
        + "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
        + "</tbody></table>"
    )


# ---- data ------------------------------------------------------------------------------------


def load(conn: psycopg.Connection) -> dict:
    cur = conn.cursor(row_factory=dict_row)
    fv = cur.execute("select max(feed_version_id) as fv from gtfs.feed_version").fetchone()["fv"]
    routes = cur.execute(
        "select route_id, route_short_name, route_long_name from gtfs.routes "
        "where feed_version_id = %s",
        (fv,),
    ).fetchall()
    stops = cur.execute(
        "select stop_id, stop_code, stop_name from gtfs.stops "
        "where feed_version_id = %s and location_type = 0",
        (fv,),
    ).fetchall()
    lat = cur.execute(
        """
        select route_id, stop_id, hour_local, n, median_delay_s, p10_delay_s, p90_delay_s,
               mean_abs_delay_s
        from marts.mart_lateness where period = '30d' and days = 'all'
        """
    ).fetchall()
    # each route at each stop (and its place along the route), and each stop by hour
    pairs = cur.execute(
        """
        select case when grouping(route_id) = 0 then route_id end as route_id, stop_id,
               case when grouping(hour_local) = 0 then hour_local end as hour_local,
               min(stop_sequence) as seq, count(*) as n,
               percentile_cont(0.5) within group (order by delay_s) as median_delay_s,
               percentile_cont(0.1) within group (order by delay_s) as p10_delay_s,
               percentile_cont(0.9) within group (order by delay_s) as p90_delay_s,
               avg(abs(delay_s)) as mean_abs_delay_s
        from marts.fct_stop_events
        where status is not null
          and service_date >= (now() at time zone 'America/Los_Angeles')::date - 29
        group by grouping sets ((route_id, stop_id), (stop_id, hour_local))
        """
    ).fetchall()
    since = "(now() at time zone 'America/Los_Angeles')::date - 29"
    pred_routes = cur.execute(
        f"""
        select route_id,
               sum(sum_abs_s) filter (where basis = 'sign' and ahead_min = 5)::float8
                   / nullif(sum(n) filter (where basis = 'sign' and ahead_min = 5), 0) as pred_s,
               sum(sum_abs_s) filter (where basis = 'timetable')::float8
                   / nullif(sum(n) filter (where basis = 'timetable'), 0) as tt_s
        from marts.mart_prediction_daily where service_date >= {since}
        group by 1
        """
    ).fetchall()
    pred_stops = cur.execute(
        f"""
        select stop_id,
               sum(sum_abs_s) filter (where basis = 'sign' and ahead_min = 5)::float8
                   / nullif(sum(n) filter (where basis = 'sign' and ahead_min = 5), 0) as pred_s,
               sum(sum_abs_s) filter (where basis = 'timetable')::float8
                   / nullif(sum(n) filter (where basis = 'timetable'), 0) as tt_s,
               sum(n) filter (where basis = 'timetable') as n
        from marts.mart_prediction_stop_daily where service_date >= {since}
        group by 1
        """
    ).fetchall()
    return {
        "routes": routes,
        "stops": stops,
        "lat": lat,
        "pairs": pairs,
        "pred_routes": {r["route_id"]: r for r in pred_routes},
        "pred_stops": {r["stop_id"]: r for r in pred_stops if (r["n"] or 0) >= MIN_ARRIVALS},
    }


def route_slug(short: str) -> str:
    return quote(str(short).strip().lower().replace(" ", "-"))


def route_sort(short: str) -> tuple:
    s = str(short)
    return (len(s) if s[:1].isdigit() else 99, s.lower())


# ---- build -----------------------------------------------------------------------------------


def build(database_url: str, target: Path | None = None) -> int:
    """Write every page; returns how many. Pages for routes and stops no longer measured are
    removed."""
    target = target or out_dir()
    base = base_url()
    now = datetime.now(tz=LOCAL_TZ)
    updated = now.strftime("%b %-d, %Y")
    with psycopg.connect(database_url) as conn:
        d = load(conn)

    route_by_id = {r["route_id"]: r for r in d["routes"]}
    stop_by_id = {}
    for s in d["stops"]:
        if not s["stop_code"] or "arrival zone" in s["stop_name"].lower():
            continue
        stop_by_id[s["stop_id"]] = {**s, "name": clean_stop_name(s["stop_name"])}
    overall = next(
        (
            r
            for r in d["lat"]
            if r["route_id"] is None and r["stop_id"] is None and r["hour_local"] is None
        ),
        None,
    )
    route_rows = {
        r["route_id"]: r
        for r in d["lat"]
        if r["route_id"] and r["stop_id"] is None and r["hour_local"] is None
    }
    route_hours: dict[str, list[dict]] = {}
    for r in d["lat"]:
        if r["route_id"] and r["hour_local"] is not None:
            route_hours.setdefault(r["route_id"], []).append(r)
    stop_rows = {
        r["stop_id"]: r
        for r in d["lat"]
        if r["stop_id"] and r["stop_id"] in stop_by_id and r["n"] >= MIN_ARRIVALS
    }
    at_stop: dict[str, list[dict]] = {}
    on_route: dict[str, list[dict]] = {}
    stop_hours: dict[str, list[dict]] = {}
    for r in d["pairs"]:
        if r["route_id"] is None:
            stop_hours.setdefault(r["stop_id"], []).append(r)
        elif r["n"] >= MIN_ARRIVALS:
            at_stop.setdefault(r["stop_id"], []).append(r)
            on_route.setdefault(r["route_id"], []).append(r)

    def route_name(rid: str) -> str:
        r = route_by_id.get(rid) or {}
        return str(r.get("route_short_name") or rid)

    def route_full(rid: str) -> str:
        r = route_by_id.get(rid) or {}
        long = r.get("route_long_name") or ""
        short = route_name(rid)
        return f"{short} ({long})" if long and long != short else short

    def stop_href(sid: str) -> str:
        return f"/reports/stop/{quote(stop_by_id[sid]['stop_code'])}/"

    def route_href(rid: str) -> str:
        return f"/reports/route/{route_slug(route_name(rid))}/"

    written: set[Path] = set()

    def write(path: str, html: str) -> None:
        f = target / path.strip("/") / "index.html" if path.endswith("/") else target / path
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(html)
        written.add(f)

    # one page per route
    for rid, r in route_rows.items():
        if rid not in route_by_id:
            continue
        short, full = route_name(rid), route_full(rid)
        hours = route_hours.get(rid, [])
        worst = max(
            (h for h in hours if h["n"] >= 10), key=lambda h: h["median_delay_s"], default=None
        )
        lede = (
            f"Over the last 30 days, the typical route {escape(short)} bus was "
            f"{delay_words(r['median_delay_s'])} against the timetable, and 8 in 10 buses came "
            f"{range_words(r['p10_delay_s'], r['p90_delay_s'])}. On average the buses were "
            f"{off_words(r['mean_abs_delay_s'])} off the timetable, early or late alike."
            + (
                f" The latest hour was {hour_label(worst['hour_local'])}, when the typical bus "
                f"was {delay_words(worst['median_delay_s'])}."
                if worst
                else ""
            )
            + prediction_sentence(d["pred_routes"].get(rid))
        )
        stops_here = sorted(
            (p for p in on_route.get(rid, []) if p["stop_id"] in stop_by_id),
            key=lambda p: (p["seq"], stop_by_id[p["stop_id"]]["name"]),
        )
        rows = [
            [
                f"<a href='{stop_href(p['stop_id'])}'>{escape(stop_by_id[p['stop_id']]['name'])}</a>",
                delay_words(p["median_delay_s"]),
                range_short(p["p10_delay_s"], p["p90_delay_s"]),
                off_words(p["mean_abs_delay_s"]),
            ]
            for p in stops_here
        ]
        chart = hour_chart(hours)
        body = (
            f"<h1>How reliable is route {escape(full)}?</h1>"
            f"<p class='lede'>{lede}</p>"
            f"<p><a class='go' href='/route?route={quote(str(rid))}'>Open the interactive report "
            "card →</a></p>"
            f"<div class='card'><h2>Route {escape(short)} at a glance</h2>"
            + numbers(r, " vs timetable")
            + "</div>"
            + (
                f"<div class='card'><h2>How late are the buses on route {escape(short)}, by "
                f"hour?</h2>{chart}</div>"
                if chart
                else ""
            )
            + (
                f"<div class='card'><h2>How reliable is each stop on route {escape(short)}?</h2>"
                + table(["Stop", "Typical bus", "8 in 10 buses", "Minutes off"], rows)
                + "</div>"
                if rows
                else ""
            )
        )
        write(
            route_href(rid),
            page(
                f"Route {full}: how late is the bus? · Eugene Bus Watch",
                f"How reliable LTD route {full} is in Eugene, Oregon: the typical bus was "
                f"{delay_words(r['median_delay_s'])} over the last 30 days. Lateness by hour and "
                "stop, and how far off the real-time predictions are.",
                route_href(rid),
                body,
                base,
                updated,
            ),
        )

    # one page per stop
    for sid, r in stop_rows.items():
        s = stop_by_id[sid]
        name, code = s["name"], s["stop_code"]
        routes_here = sorted(
            (p for p in at_stop.get(sid, []) if p["route_id"] in route_by_id),
            key=lambda p: route_sort(route_name(p["route_id"])),
        )
        names_here = ", ".join(route_name(p["route_id"]) for p in routes_here)
        lede = (
            f"Over the last 30 days, the typical bus at {escape(name)} (stop #{escape(code)}"
            + (
                f"; route{'s' if len(routes_here) > 1 else ''} {escape(names_here)}"
                if names_here
                else ""
            )
            + f") was {delay_words(r['median_delay_s'])} against the timetable, and 8 in 10 buses "
            f"came {range_words(r['p10_delay_s'], r['p90_delay_s'])}. On average the buses were "
            f"{off_words(r['mean_abs_delay_s'])} off the timetable, early or late alike."
            + prediction_sentence(d["pred_stops"].get(sid))
        )
        rows = [
            [
                f"<a href='{route_href(p['route_id'])}'>Route {escape(route_name(p['route_id']))}</a>",
                delay_words(p["median_delay_s"]),
                range_short(p["p10_delay_s"], p["p90_delay_s"]),
                off_words(p["mean_abs_delay_s"]),
            ]
            for p in routes_here
        ]
        chart = hour_chart(stop_hours.get(sid, []))
        body = (
            f"<h1>How reliable is the stop at {escape(name)}?</h1>"
            f"<p class='small'>Stop #{escape(code)}</p>"
            f"<p class='lede'>{lede}</p>"
            f"<p><a class='go' href='/stops?stop={quote(str(sid))}'>Open the interactive report "
            "card →</a></p>"
            f"<div class='card'><h2>{escape(name)} at a glance</h2>"
            + numbers(r, " here vs timetable")
            + "</div>"
            + (
                f"<div class='card'><h2>How late are the buses at {escape(name)}, by hour?</h2>"
                f"{chart}</div>"
                if chart
                else ""
            )
            + (
                f"<div class='card'><h2>How reliable is each route at {escape(name)}?</h2>"
                + table(["Route", "Typical bus", "8 in 10 buses", "Minutes off"], rows)
                + "</div>"
                if rows
                else ""
            )
        )
        write(
            stop_href(sid),
            page(
                f"{name} (stop #{code}): how late is the bus? · Eugene Bus Watch",
                f"How reliable LTD buses are at {name} (stop #{code}) in Eugene/Springfield, "
                f"Oregon: the typical bus was {delay_words(r['median_delay_s'])} over the last 30 "
                "days. Each route here, lateness by hour, and prediction accuracy.",
                stop_href(sid),
                body,
                base,
                updated,
            ),
        )

    # the index of them all
    route_list = sorted(
        (rid for rid in route_rows if rid in route_by_id), key=lambda x: route_sort(route_name(x))
    )
    rrows = [
        [
            f"<a href='{route_href(rid)}'>Route {escape(route_full(rid))}</a>",
            delay_words(route_rows[rid]["median_delay_s"]),
            range_short(route_rows[rid]["p10_delay_s"], route_rows[rid]["p90_delay_s"]),
            off_words(route_rows[rid]["mean_abs_delay_s"]),
        ]
        for rid in route_list
    ]
    stop_list = sorted(stop_rows, key=lambda x: stop_by_id[x]["name"].lower())
    links = "".join(
        f"<a href='{stop_href(sid)}'>{escape(stop_by_id[sid]['name'])} "
        f"<span class='small'>#{escape(stop_by_id[sid]['stop_code'])}</span></a>"
        for sid in stop_list
    )
    lede = (
        "How late Lane Transit District buses run in Eugene and Springfield, Oregon, route by "
        "route and stop by stop, measured against the printed timetable."
        + (
            f" Over the last 30 days the typical bus was {delay_words(overall['median_delay_s'])}, "
            f"and 8 in 10 buses came {range_words(overall['p10_delay_s'], overall['p90_delay_s'])}."
            if overall
            else ""
        )
    )
    write(
        "/reports/",
        page(
            "Every LTD route and stop: how late are the buses? · Eugene Bus Watch",
            "Reliability report cards for every Lane Transit District route and stop in Eugene "
            "and Springfield, Oregon: how late the buses really run, measured from live bus "
            "positions.",
            "/reports/",
            f"<h1>How reliable is every route and stop?</h1><p class='lede'>{lede}</p>"
            "<p><a class='go' href='/'>Open the interactive site →</a></p>"
            "<div class='card'><h2>Every route</h2>"
            + table(["Route", "Typical bus", "8 in 10 buses", "Minutes off"], rrows)
            + f"</div><div class='card'><h2>Every stop</h2>"
            f"<div class='cols'>{links}</div></div>",
            base,
            updated,
        ),
    )

    # sitemap and robots.txt (absolute addresses need the site's name)
    if base:
        today = date.today().isoformat()
        pages = ["/", "/routes", "/stops", "/accuracy", "/map", "/methods", "/reports/"]
        pages += [route_href(rid) for rid in route_list] + [stop_href(sid) for sid in stop_list]
        write(
            "sitemap.xml",
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            + "".join(
                f"<url><loc>{escape(base + p)}</loc><lastmod>{today}</lastmod></url>\n"
                for p in pages
            )
            + "</urlset>\n",
        )
    write(
        "robots.txt",
        "User-agent: *\nAllow: /\nDisallow: /status\n"
        + (f"Sitemap: {base}/sitemap.xml\n" if base else ""),
    )

    # pages for routes or stops no longer measured
    for f in (target / "reports").rglob("index.html"):
        if f not in written:
            f.unlink()
    log.info("static pages written: %d (to %s)", len(written), target)
    return len(written)
