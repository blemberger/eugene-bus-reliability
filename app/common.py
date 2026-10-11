"""Shared helpers for the dashboard: database access, formatting, filters."""

from __future__ import annotations

import inspect
import math
import os
import queue
import re
import threading
import time
import uuid
from datetime import date, datetime, timedelta
from datetime import time as dtime
from html import escape as html_escape
from pathlib import Path
from urllib.parse import quote

import pandas as pd
import plotly.graph_objects as go
import psycopg
import streamlit as st


def _database_url() -> str:
    return os.environ.get("DATABASE_URL", "")


DATABASE_URL = _database_url()
LOCAL_TZ = "America/Los_Angeles"

STATUS_LABEL = {"early": "Early", "on_time": "On time", "late": "Late", "very_late": "Very late"}
STATUS_COLOR = {
    "early": "#e6a100",
    "on_time": "#2e8b57",
    "late": "#d9534f",
    "very_late": "#8b0000",
    "unknown": "#9e9e9e",
}
ROUTE_PALETTE = [
    "#1f77b4",
    "#ff7f0e",
    "#2ca02c",
    "#d62728",
    "#9467bd",
    "#8c564b",
    "#e377c2",
    "#7f7f7f",
    "#bcbd22",
    "#17becf",
    "#393b79",
    "#637939",
    "#8c6d31",
    "#843c39",
    "#7b4173",
    "#3182bd",
    "#e6550d",
    "#31a354",
    "#756bb1",
    "#636363",
    "#6baed6",
    "#fd8d3c",
    "#74c476",
    "#9e9ac8",
    "#969696",
    "#9ecae1",
    "#fdae6b",
    "#a1d99b",
]


# ---------------------------------------------------------------- database ----


def require_db() -> None:
    if not DATABASE_URL:
        st.error("DATABASE_URL is not set. Run via `make app` so .env is loaded.")
        st.stop()


_COMPASS = {
    "N": "north",
    "S": "south",
    "E": "east",
    "W": "west",
    "NE": "northeast",
    "NW": "northwest",
    "SE": "southeast",
    "SW": "southwest",
}
_STOP_WORDS = {
    "SPFLD": "Springfield",
    "STA": "Station",
    "STN": "Station",
    "CTR": "Center",
    "MKTPL": "Marketplace",
    "HWY": "Hwy",
    "EUG": "Eugene",
}
_SIDE = r"(?P<side>[NSEW])/\s?S\.?\s+of\s+"
_STOP_RULES = [
    (
        "side, street, direction of cross street",
        re.compile(
            _SIDE + r"(?P<street>.+?)\s+(?P<dir>NE|NW|SE|SW|N|S|E|W)\.?\s+of\s+(?P<cross>.+)$", re.I
        ),
        "{street} {dir} of {cross} ({side} side)",
    ),
    (
        "side, street at cross street",
        re.compile(_SIDE + r"(?P<street>.+?)\s+(?:at|@|&|and)\s+(?P<cross>.+)$", re.I),
        "{street} at {cross} ({side} side)",
    ),
    ("side, street only", re.compile(_SIDE + r"(?P<street>.+)$", re.I), "{street} ({side} side)"),
]
STOP_NAME_COLUMNS = ("stop_name", "next_stop", "stop2", "name1", "name2")


def _tidy_words(s: str) -> str:
    out = []
    for w in s.split():
        key = w.strip(".,").upper()
        if key in _STOP_WORDS:
            out.append(_STOP_WORDS[key])
        elif w.isupper() and len(w) > 3 and w.isalpha():  # "HILYARD" -> "Hilyard"; keep "LCC", "UO"
            out.append(w.capitalize())
        else:
            out.append(w)
    return " ".join(out)


def stop_name_rule(name) -> tuple[str, str | None]:
    """(readable name, the rule that produced it or None if no rule matched)."""
    if name is None or (isinstance(name, float) and math.isnan(name)):
        return "", None
    raw = " ".join(str(name).split())
    for label, rx, template in _STOP_RULES:
        m = rx.match(raw)
        if m:
            parts = {k: _tidy_words(v) for k, v in m.groupdict().items()}
            parts["side"] = m.group("side").upper()  # "(S side)": short, it's on every stop
            if "dir" in parts:
                parts["dir"] = _COMPASS[m.group("dir").upper()]
            return template.format(**parts), label
    return _tidy_words(raw), None


def clean_stop_name(name) -> str:
    return stop_name_rule(name)[0]


def readable_stop_names(df: pd.DataFrame) -> pd.DataFrame:
    for col in STOP_NAME_COLUMNS:
        if col in df.columns:
            df[col] = df[col].map(
                lambda v: (
                    v
                    if v is None or (isinstance(v, float) and math.isnan(v))
                    else clean_stop_name(v)
                )
            )
    return df


_SEARCH_FILLER = {
    "north",
    "south",
    "east",
    "west",
    "northeast",
    "northwest",
    "southeast",
    "southwest",
    "of",
    "at",
    "and",
    "&",
    "side",
    "the",
    "near",
    "street",
    "st",
    "ave",
    "avenue",
    "stop",
    "(west",
    "(east",
    "(north",
    "(south",
    "side)",
}


def search_stops(query: str, limit: int = 12) -> pd.DataFrame:
    """Stops matching a typed search: a sign number or stop id, or all the words given."""
    fv = current_fv()
    text = query.strip()
    words = [w.strip("()") for w in text.split()]
    words = [w for w in words if w and w.lower() not in _SEARCH_FILLER]
    name_clause = " and ".join(["s.stop_name ilike %s"] * len(words)) or "false"
    return q(
        f"""
        select s.stop_id, s.stop_code, s.stop_name from gtfs.stops s
        where s.feed_version_id = {fv} and s.location_type = 0
          and (ltrim(s.stop_code, '0') = ltrim(%s, '0') or s.stop_id = %s or ({name_clause}))
        order by length(s.stop_name), s.stop_name limit {int(limit)}
        """,
        (text, text, *[f"%{w}%" for w in words]),
    )


# Every query the pages run, with how long it took: app/selfcheck.py reports the slowest per
# page, and any over SLOW_QUERY_SECONDS is logged (the dump's LOG LINES section shows it).
QUERY_LOG: list[tuple[float, str]] = []
SLOW_QUERY_SECONDS = 1.0


def _query_label(sql: str) -> str:
    return " ".join(sql.split())[:110]


# Open database connections kept for reuse: a new connection costs a login (password hashing
# and a server process) every time, and a page runs a dozen or more queries.
_POOL: queue.LifoQueue = queue.LifoQueue(maxsize=6)


def _connection() -> psycopg.Connection:
    while True:
        try:
            conn = _POOL.get_nowait()
        except queue.Empty:
            return psycopg.connect(DATABASE_URL, connect_timeout=5)
        if not conn.closed and conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE:
            return conn
        conn.close()


def _release(conn: psycopg.Connection) -> None:
    try:
        _POOL.put_nowait(conn)
    except queue.Full:
        conn.close()


def _run(sql: str, params: tuple = ()) -> pd.DataFrame:
    """Run one read-only query and return a DataFrame (no caching)."""
    t0 = time.monotonic()
    conn = _connection()
    try:
        with conn.transaction(), conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            cur.execute(sql, params)
            cols = [d.name for d in cur.description]
            rows = cur.fetchall()
    except BaseException:
        conn.close()  # a failed or interrupted connection is not reused
        raise
    _release(conn)
    df = readable_stop_names(pd.DataFrame(rows, columns=cols))
    seconds = time.monotonic() - t0
    QUERY_LOG.append((seconds, _query_label(sql)))
    del QUERY_LOG[:-500]
    if seconds > SLOW_QUERY_SECONDS:
        print(f"WARNING slow query {seconds:.1f} s: {_query_label(sql)}", flush=True)
    return df


@st.cache_data(ttl=30, show_spinner=False)
def build_marker() -> str:
    """When the latest analysis build finished. Everything the pages read from the analysis
    (and the schedule) changes only then, so those queries are cached until the next build."""
    try:
        df = _run("select max(finished_at)::text as at from analytics.build_log")
        return str(df["at"][0])
    except psycopg.Error:
        return ""


_RAW_TABLES = re.compile(r"\brt\.")


@st.cache_data(ttl=60, show_spinner=False)
def _q_raw(sql: str, params: tuple) -> pd.DataFrame:
    return _run(sql, params)


@st.cache_data(ttl=3600, show_spinner=False, max_entries=2000)
def _q_built(sql: str, params: tuple, marker: str) -> pd.DataFrame:
    return _run(sql, params)


# The analysis queries visitors have run recently (query and parameters -> when last used), so
# warm_caches() can run them again as soon as a new analysis build lands: the next visitor then
# finds them ready instead of waiting for the database.
_RECENT_QUERIES: dict[tuple[str, tuple], float] = {}
WARM_WITHIN_HOURS = 24
WARM_MAX_QUERIES = 400


def q(sql: str, params: tuple = ()) -> pd.DataFrame:
    """Run a read-only query and return a DataFrame. Queries on the raw realtime tables (rt.*)
    are cached for 60 s; everything else until the next analysis build (at most an hour)."""
    if _RAW_TABLES.search(sql):
        return _q_raw(sql, params)
    try:
        _RECENT_QUERIES[(sql, params)] = time.time()
    except TypeError:  # parameters that can't be a key (a list): not warmed
        pass
    return _q_built(sql, params, build_marker())


def warm_caches() -> None:
    """Forever, in a background thread: when a new analysis build finishes, re-run the queries
    visitors used in the last day (most recent first) so their results are cached before anyone
    asks. Started once per server by streamlit_app.py."""
    warmed = None
    while True:
        time.sleep(20)
        try:
            marker = build_marker()
            if marker == warmed or not marker:
                continue
            cutoff = time.time() - WARM_WITHIN_HOURS * 3600
            for key, used in list(_RECENT_QUERIES.items()):
                if used < cutoff:
                    _RECENT_QUERIES.pop(key, None)
            recent = sorted(_RECENT_QUERIES.items(), key=lambda kv: -kv[1])[:WARM_MAX_QUERIES]
            t0 = time.monotonic()
            for (sql, params), _ in recent:
                if build_marker() != marker:  # a newer build landed meanwhile: start over
                    break
                try:
                    _q_built(sql, params, marker)
                except Exception:  # noqa: BLE001 — one failing query must not stop the rest
                    pass
                time.sleep(0.05)  # leave the database room for visitors
            else:
                warmed = marker
                if recent:
                    print(
                        f"cache warmer: {len(recent)} queries ready for the build of {marker} "
                        f"in {time.monotonic() - t0:.0f} s",
                        flush=True,
                    )
        except Exception:  # noqa: BLE001 — keep warming on the next build
            pass


@st.cache_resource
def start_cache_warmer() -> threading.Thread:
    t = threading.Thread(target=warm_caches, name="cache-warmer", daemon=True)
    t.start()
    return t


@st.cache_data(ttl=10, show_spinner=False)
def q_live(sql: str, params: tuple = ()) -> pd.DataFrame:
    """Same as q() with a 10 s cache, for parts of the page that refresh themselves."""
    return _run(sql, params)


# Every live view shows the data "as of" the newest message (data_now), not the wall clock,
# so a page changes only when new data arrives, at the moment the countdown resets. Pages
# check for new data every LIVE_CHECK_SECONDS; the marker itself is cached for 1 s.
POLL_SECONDS = 30
LIVE_CHECK_SECONDS = 2


@st.cache_data(ttl=1, show_spinner=False)
def live_marker() -> dict:
    """Id and arrival time of the newest realtime message stored."""
    row = _run(
        """
        with recent as (
            select fetch_id, fetched_at from rt.fetch
            where feed = 'vehicle_positions' and fetched_at > now() - interval '1 day'
            order by fetched_at desc limit 6
        )
        select (select max(fetch_id) from rt.fetch
                where feed in ('vehicle_positions', 'trip_updates')
                  and fetched_at > now() - interval '1 day') as fid,
               (select max(fetched_at) from rt.fetch
                where feed in ('vehicle_positions', 'trip_updates')
                  and fetched_at > now() - interval '1 day') as at,
               (select percentile_cont(0.5) within group (order by gap) from (
                    select extract(epoch from fetched_at - lag(fetched_at) over (order by fetched_at)) as gap
                    from recent) g where gap is not null) as gap_s
        """
    ).iloc[0]
    gap = row["gap_s"]
    return {
        "fid": None if pd.isna(row["fid"]) else int(row["fid"]),
        "at": row["at"],
        # the poller's actual rhythm (normally ~30 s), measured from the last few messages
        "interval": float(gap)
        if gap is not None and not pd.isna(gap) and 5 < gap < 120
        else POLL_SECONDS,
    }


def data_now() -> pd.Timestamp:
    """The time live views are drawn 'as of': when the newest message arrived (UTC)."""
    m = live_marker()
    if m["at"] is None or pd.isna(m["at"]):
        return pd.Timestamp.now(tz="UTC")
    return pd.Timestamp(m["at"]).tz_convert("UTC")


@st.cache_data(ttl=600, show_spinner=False)
def q_fresh(sql: str, params: tuple = (), marker: int | None = None) -> pd.DataFrame:
    """q() for live data, re-run only when `marker` (live_marker()['fid']) changes."""
    return _run(sql, params)


def _countdown_html() -> str:
    m = live_marker()
    if m["at"] is None or pd.isna(m["at"]):
        return (
            "<div style='font-size:20px;font-weight:700;color:#b35c00;'>"
            "Live · waiting for the first message from LTD's feed</div>"
        )
    at = pd.Timestamp(m["at"])
    now = pd.Timestamp.now(tz="UTC")
    # The countdown reaches 0 when the page is expected to show the next data: one poll interval
    # after the last message, plus the page's pickup delay (it checks every LIVE_CHECK_SECONDS).
    period = m["interval"] + 1.5  # measured: the page shows new data 0-3 s after it lands
    due = at + pd.Timedelta(seconds=period)
    left = int((due - now).total_seconds())
    done = max(0.0, min(100.0, 100 * (now - at).total_seconds() / period))

    def local(t: pd.Timestamp) -> str:
        return (
            t.tz_convert(LOCAL_TZ).strftime("%-I:%M:%S %p").replace("AM", "am").replace("PM", "pm")
        )

    if left > 0:
        headline, colour = f"Next update in {left} s", "#0b6e4f"
    elif left > -60:
        headline, colour = "Updating…", "#0b6e4f"
    else:  # well past due: say so plainly instead of sitting at 'Updating…'
        headline, colour = (
            f"No new data from LTD for {int((now - at).total_seconds() // 60)} min",
            "#b35c00",
        )
    # a small ring that fills up toward the next update, on one line with the text
    # (CSS, not SVG: st.html's sanitiser removes inline SVG)
    ring = (
        "<span style='flex:none;display:inline-block;width:22px;height:22px;border-radius:50%;"
        f"background:conic-gradient({colour} {done:.1f}%, #dfece5 0);"
        "-webkit-mask:radial-gradient(farthest-side,transparent calc(100% - 5px),#000 calc(100% - 4px));"
        "mask:radial-gradient(farthest-side,transparent calc(100% - 5px),#000 calc(100% - 4px))'></span>"
    )
    return f"""<div style="display:flex;flex-wrap:wrap;align-items:center;gap:4px 10px;margin:0 0 4px 0;">
  {ring}<span style="font-size:16px;font-weight:700;color:{colour};">{headline}</span>
  <span style="font-size:14px;color:#666;">LTD data from {local(at)} · next update due about {local(due)}</span>
</div>"""


@st.fragment(run_every="1s")
def live_status_line() -> None:
    """A compact countdown to LTD's next update (a filling ring) and the time of the data."""
    st.html(_countdown_html())


def stop_link(stop_id, name) -> str | None:
    """A link to a stop's page, for LinkColumn (the text after # is what the cell shows)."""
    if stop_id is None or pd.isna(stop_id):
        return None
    return f"/stops?stop={quote(str(stop_id))}#{'' if name is None or pd.isna(name) else name}"


def route_link(route_id, name) -> str | None:
    """A link to a route's report card, for LinkColumn (the text after # is what the cell shows)."""
    if route_id is None or pd.isna(route_id):
        return None
    return f"/route?route={quote(str(route_id))}#{'' if name is None or pd.isna(name) else name}"


# Links that open another page (a route's or a stop's report card) look like cards with an
# arrow, never like the rounded buttons that change what the current page shows. Route cards are
# edged in blue, stop cards in green, the same on every page. The cards fill whole rows: 7 routes
# a row on a computer (LTD's 28 routes make 4 full rows), 4 stops (12 busy stops, 3 rows).
ROUTE_TILE_COLOUR = "#1f5f9e"
STOP_TILE_COLOUR = "#0b6e4f"
_TILE_CSS = (
    "<style>.ebw-tiles { display: grid; gap: 0.5rem; }"
    ".ebw-tiles.route { grid-template-columns: repeat(7, minmax(0, 1fr)); }"
    ".ebw-tiles.stop { grid-template-columns: repeat(4, minmax(0, 1fr)); }"
    "@media (max-width: 1100px) { .ebw-tiles.route { grid-template-columns: repeat(4, minmax(0, 1fr)); }"
    " .ebw-tiles.stop { grid-template-columns: repeat(3, minmax(0, 1fr)); } }"
    "@media (max-width: 800px) { .ebw-tiles.stop { grid-template-columns: repeat(2, minmax(0, 1fr)); } }"
    "@media (max-width: 640px) { .ebw-tiles.route { grid-template-columns: repeat(2, minmax(0, 1fr)); }"
    " .ebw-tiles.stop { grid-template-columns: minmax(0, 1fr); } }"
    ".ebw-tile { display: flex; align-items: center; gap: 0.5rem; padding: 0.45rem 0.65rem;"
    " border: 1px solid #c9d2cd; border-left: 4px solid var(--edge); border-radius: 6px;"
    " background: #fff; color: #262730; text-decoration: none; line-height: 1.25; min-width: 0; }"
    ".ebw-tile:hover { background: #f1f5f8; border-color: var(--edge); }"
    ".ebw-tile b { font-size: 1rem; }"
    ".ebw-tile > span:first-child { min-width: 0; overflow: hidden; }"
    ".ebw-tile small { display: block; color: #666; font-size: 0.85rem; white-space: nowrap;"
    " overflow: hidden; text-overflow: ellipsis; }"
    # a long stop name wraps (all tiles in a row grow together) rather than hiding the number
    ".ebw-tile .ebw-one { display: block; overflow-wrap: anywhere; }"
    ".ebw-tile .ebw-code { color: #666; font-weight: 400; white-space: nowrap; }"
    ".ebw-tile .ebw-go { margin-left: auto; color: var(--edge); font-weight: 700; font-size: 1.05rem; }"
    "</style>"
)


def nav_tiles(items: list[tuple[str, str]], kind: str) -> None:
    """Cards that each open another page: (href, inner HTML), kind 'route' or 'stop'. The
    page's period and days filters go along."""
    qs = filter_query()
    edge = ROUTE_TILE_COLOUR if kind == "route" else STOP_TILE_COLOUR
    tiles = "".join(
        f"<a class='ebw-tile' target='_self' title='Open the report card' "
        f"href='{html_escape(href + (('&' if '?' in href else '?') + qs if qs else ''))}'>"
        f"<span>{inner}</span><span class='ebw-go'>→</span></a>"
        for href, inner in items
    )
    st.html(f"{_TILE_CSS}<div class='ebw-tiles {kind}' style='--edge:{edge}'>{tiles}</div>")


def route_tiles(routes: pd.DataFrame) -> None:
    """A compact card per route (route_id, route_short_name, and route_long_name if known),
    each opening that route's report card."""
    names = routes["route_long_name"] if "route_long_name" in routes else [""] * len(routes)
    nav_tiles(
        [
            (
                f"/route?route={quote(str(rid))}",
                f"<b>Route {html_escape(str(short))}</b>"
                + (
                    f"<small>{html_escape(long)}</small>"
                    if isinstance(long, str) and long and long != str(short)
                    else "<small>&nbsp;</small>"
                ),
            )
            for rid, short, long in zip(
                routes["route_id"], routes["route_short_name"], names, strict=False
            )
        ],
        "route",
    )


def stop_tiles(stops: pd.DataFrame, show_code: bool = True) -> None:
    """A one-line card per stop (stop_id, stop_name, stop_code): its name and sign number, each
    opening that stop's report card."""
    nav_tiles(
        [
            (
                f"/stops?stop={quote(str(r.stop_id))}",
                f"<span class='ebw-one'><b>{html_escape(str(r.stop_name))}</b>"
                + (
                    f" <span class='ebw-code'>· #{html_escape(str(r.stop_code))}</span>"
                    if show_code and r.stop_code
                    else ""
                )
                + "</span>",
            )
            for r in stops.itertuples()
        ],
        "stop",
    )


def back_button(label: str, page: str | None = None, on_click=None) -> None:
    """The way back from a report card to the list of every route or stop: an outlined button
    above the title (styled by its st-key-backlink container in streamlit_app.py). A page link
    when the list is another page, a button (with on_click) when it is the same page."""
    with st.container(key=f"backlink_{re.sub(r'[^a-z]', '', label.lower())}"):
        if page:
            st.page_link(page, label=label)
        else:
            st.button(label, on_click=on_click)


def col_route(label: str = "Route"):
    return st.column_config.LinkColumn(
        label, display_text=r"#(.*)$", help="Opens this route's report card (in a new tab)."
    )


# The site's two measures of reliability, everywhere the same words (RANGE_HELP is below):
#   Typical bus    — the median minutes behind the timetable (negative = early)
#   8 in 10 buses  — the 10th to 90th percentile of the same, as a range
TYPICAL_HELP = "Median minutes behind the timetable: half the buses were later than this, half earlier. Negative = early."


def col_typical(label: str = "Typical bus vs timetable", max_minutes: float = 5.0):
    """Typical minutes late, drawn as a bar from 0 (on schedule) up to max_minutes."""
    return st.column_config.ProgressColumn(
        label, format="%.1f min", min_value=0.0, max_value=float(max_minutes), help=TYPICAL_HELP
    )


def col_stop(label: str = "Stop"):
    return st.column_config.LinkColumn(
        label, display_text=r"#(.*)$", help="Opens this stop's page (in a new tab)."
    )


HONEST_MIN_N = 50
HORIZON_BANDS = (1, 3, 6, 10, 15, 20)  # else 30; must match the dbt macro horizon_band()


def horizon_band(minutes: float) -> int:
    for b in HORIZON_BANDS:
        if minutes <= b:
            return b
    return 30


def day_part(hour: int) -> str:
    return (
        "morning"
        if hour < 9
        else "midday"
        if hour < 15
        else "afternoon"
        if hour < 19
        else "evening"
    )


@st.cache_data(ttl=300, show_spinner=False)
def error_ranges() -> dict:
    """{(route_id|None, day_part|None, is_timepoint, band): (p10, p50, p90) seconds}."""
    if not marts_ready():
        return {}
    exists = q("select to_regclass('marts.mart_prediction_error_ranges') is not null as ok")
    if not bool(exists["ok"][0]):
        return {}
    df = q(
        "select * from marts.mart_prediction_error_ranges where n_predictions >= %s",
        (HONEST_MIN_N,),
    )
    return {
        (
            None if pd.isna(r.route_id) else r.route_id,
            None if pd.isna(r.day_part) else r.day_part,
            bool(r.is_timepoint),
            int(r.horizon_band),
        ): (r.p10_error_s, r.p50_error_s, r.p90_error_s)
        for r in df.itertuples()
    }


def add_honest_columns(
    df: pd.DataFrame, minutes: str, route_id: str, is_timepoint: str
) -> pd.DataFrame:
    """Adds likely_min (LTD's time + the usual error) and usual_range ('6–9 min', the 80%
    range) to rows with a minutes-until column. Empty where there's no history yet."""
    ranges = error_ranges()
    part = day_part(now_local().hour)
    likely, text = [], []
    for m, rid, tp in zip(df[minutes], df[route_id], df[is_timepoint], strict=False):
        if m is None or pd.isna(m):
            likely.append(None)
            text.append("")
            continue
        band, tpb = (
            horizon_band(float(m)),
            bool(tp) if tp is not None and not pd.isna(tp) else False,
        )
        hit = (
            ranges.get((rid, part, tpb, band))
            or ranges.get((rid, None, tpb, band))
            or ranges.get((None, None, tpb, band))
        )
        if not hit:
            likely.append(None)
            text.append("")
            continue
        p10, p50, p90 = (float(v) / 60 for v in hit)
        likely.append(round(max(0.0, float(m) + p50)))
        lo_m, hi_m = max(0, round(float(m) + p10)), max(0, round(float(m) + p90))
        text.append(f"{lo_m} min" if lo_m == hi_m else f"{lo_m}–{hi_m} min")
    out = df.copy()
    out["likely_min"] = likely
    out["usual_range"] = text
    return out


def col_likely(label: str = "Likely in"):
    return st.column_config.NumberColumn(
        label,
        format="%.0f min",
        help="LTD's prediction corrected by how far off its predictions have usually been "
        "(same route, time of day, kind of stop and minutes ahead).",
    )


def col_usual(label: str = "80% of the time"):
    return st.column_config.TextColumn(
        label, help="In past data, the bus arrived within this range 80% of the time."
    )


def hex_to_rgb(h: str) -> list[int]:
    h = h.lstrip("#")
    return [int(h[i : i + 2], 16) for i in (0, 2, 4)]


@st.cache_data(ttl=300, show_spinner=False)
def marts_ready() -> bool:
    df = q(
        "select count(*) as n from information_schema.tables where table_schema = 'marts' and table_name = 'fct_stop_events'"
    )
    return int(df["n"][0]) > 0


@st.cache_data(ttl=300, show_spinner=False)
def current_fv() -> int:
    """feed_version_id of the schedule in force on today's service date (rolls over at 3 am)."""
    if marts_ready():
        df = q(
            "select feed_version_id from intermediate.int_feed_version_by_date "
            "where service_date = ((now() at time zone %s) - interval '3 hours')::date",
            (LOCAL_TZ,),
        )
        if not df.empty:
            return int(df["feed_version_id"][0])
    df = q("select coalesce(max(feed_version_id), 0) as fv from gtfs.feed_version")
    return int(df["fv"][0])


@st.cache_data(ttl=15, show_spinner=False)
def feed_status() -> dict:
    """Facts about LTD's live feed right now; see explain_feed()."""
    row = q(
        """
        with latest as (
            select distinct on (feed) feed::text as feed, fetched_at, entity_count
            from rt.fetch
            where feed in ('vehicle_positions', 'trip_updates') and fetched_at > now() - interval '2 days'
            order by feed, fetched_at desc
        )
        select
          (select extract(epoch from now() - max(fetched_at)) from latest) as s_since_message,
          (select coalesce(sum(entity_count), 0) from latest) as entities_in_latest,
          (select max(fetched_at) from rt.fetch
            where feed = 'vehicle_positions' and entity_count > 0
              and fetched_at > now() - interval '2 days') as last_nonempty_at
        """
    ).iloc[0]
    scheduled = None
    if marts_ready():
        scheduled = int(
            q(
                """
                select count(*) as n from intermediate.int_scheduled_stop_events
                where scheduled_arrival between now() - interval '10 minutes' and now() + interval '10 minutes'
                """
            )["n"][0]
        )
    return {
        "s_since_message": None
        if pd.isna(row["s_since_message"])
        else float(row["s_since_message"]),
        "entities_in_latest": int(row["entities_in_latest"] or 0),
        "last_nonempty_at": None if pd.isna(row["last_nonempty_at"]) else row["last_nonempty_at"],
        "scheduled_stop_events_now": scheduled,
    }


def explain_feed() -> tuple[str, str] | None:
    """(level, message) when the live feed has a problem worth telling visitors; else None."""
    s = feed_status()
    if s["s_since_message"] is None or s["s_since_message"] > 600:
        when = "never" if s["s_since_message"] is None else fmt_ago_seconds(s["s_since_message"])
        return (
            "error",
            f"Collection has stopped: the last message from LTD's feed was received {when}. "
            "Live information on this site is out of date until it restarts.",
        )
    service_running = (
        s["scheduled_stop_events_now"] > 0
        if s["scheduled_stop_events_now"] is not None
        else 6 <= now_local().hour < 22
    )
    if s["entities_in_latest"] == 0 and service_running:
        since = (
            f"since {fmt_time(s['last_nonempty_at'])}"
            if s["last_nonempty_at"] is not None
            else "for more than 2 days"
        )
        return (
            "warning",
            f"LTD's real-time feed is empty: its servers are answering, but no bus positions or "
            f"predictions have come through {since}, while buses are scheduled to be running. "
            "Live views fill in again on their own when LTD's feed returns.",
        )
    return None


def is_mobile() -> bool:
    try:
        ua = st.context.headers.get("User-Agent", "") or ""
    except Exception:  # noqa: BLE001 — no browser (tests, scripts)
        return False
    return "Mobi" in ua or "Android" in ua


# User agents of crawlers, link previewers and monitoring tools that run the site's scripts.
BOT_AGENTS = re.compile(
    r"bot|crawl|spider|slurp|headless|lighthouse|preview|monitor|python|curl|wget", re.I
)


# A browser that has opened the site once with ?dont_count_me (the site's owner, on each of
# his browsers) carries this cookie and is left out of the visitor counts, as WordPress stats
# leave out a logged-in owner. Set only on request; nothing else uses cookies.
NOT_COUNTED_COOKIE = "ebw_not_counted"


def not_counted() -> bool:
    """Is this browser left out of the visitor counts? Opening any page with ?dont_count_me
    sets that (for 10 years) and says so."""
    if "dont_count_me" in st.query_params:
        st.html(
            f"<script>document.cookie = '{NOT_COUNTED_COOKIE}=1; max-age=315360000; path=/; "
            "SameSite=Lax';</script>",
            unsafe_allow_javascript=True,
        )
        st.toast("This browser won't be counted in the visitor numbers.")
        del st.query_params["dont_count_me"]
        return True
    try:
        return st.context.cookies.get(NOT_COUNTED_COOKIE) == "1"
    except Exception:  # noqa: BLE001 — no browser (tests, scripts)
        return False


def log_page_view(page: str) -> None:
    """Count a page view in site.page_view: the time, the page (with the stop or route
    chosen on it), phone or computer, and a random id for this browser tab's session, so
    visits can be counted. No IP address, cookie or anything identifying is stored. Logged
    once per page and choice, not on every rerun; never interrupts the page. Not at all for a
    browser that asked not to be counted (not_counted())."""
    if not_counted():
        return
    try:
        ua = st.context.headers.get("User-Agent", "") or ""
    except Exception:  # noqa: BLE001 — no browser (tests, scripts)
        return
    if not ua or not DATABASE_URL:
        return
    detail = st.query_params.get("stop") or st.query_params.get("route")
    if st.session_state.get("_page_view_logged") == (page, detail):
        return
    st.session_state["_page_view_logged"] = (page, detail)
    session_id = st.session_state.setdefault("_visit_id", uuid.uuid4().hex)
    device = (
        "bot"
        if BOT_AGENTS.search(ua)
        else ("phone" if "Mobi" in ua or "Android" in ua else "computer")
    )

    def insert() -> None:
        try:
            with psycopg.connect(DATABASE_URL, connect_timeout=3) as conn:
                # the site's login is read-only by default; this table takes inserts
                conn.read_only = False
                conn.execute(
                    "insert into site.page_view (session_id, page, detail, device) "
                    "values (%s, %s, %s, %s)",
                    (session_id, page, detail, device),
                )
        except Exception:  # noqa: BLE001 — a missing table or a busy database is ignored
            pass

    # in the background: the visitor's page doesn't wait for the count
    threading.Thread(target=insert, daemon=True).start()


def fit_phone(fig):
    """On a phone, move a chart's legend from beside the plot to above it, so the plot keeps the
    screen's full width."""
    if is_mobile():
        fig.update_layout(
            legend={"orientation": "h", "yanchor": "bottom", "y": 1.02, "x": 0, "xanchor": "left"}
        )
    return fig


def fmt_ago_seconds(s: float) -> str:
    if s < 90:
        return f"{s:.0f} s ago"
    if s < 5400:
        return f"{s / 60:.0f} min ago"
    return f"{s / 3600:.1f} h ago"


def empty_message(default: str) -> str:
    """A page's 'nothing to show' text; points at the notice when the feed itself is down."""
    try:
        if explain_feed():
            return "Nothing to show right now: see the notice at the top of the page."
    except Exception:  # noqa: BLE001
        pass
    return default


def feed_banner() -> None:
    """Show a site-wide notice when LTD's live feed is down or our collection has stopped."""
    try:
        problem = explain_feed()
    except Exception:  # noqa: BLE001 — a status notice must never break a page
        return
    if problem:
        level, msg = problem
        (st.error if level == "error" else st.warning)(msg)


def require_marts() -> None:
    """Pages that need reliability numbers stop here until the analysis layer exists."""
    if not marts_ready():
        st.info(
            "Reliability numbers appear once the analysis has run on a few hours of collected "
            "bus positions. It refreshes every 15 minutes."
        )
        st.stop()


def schedule_join(pred_alias: str = "p") -> tuple[str, str]:
    """SQL fragments to attach the interpolated scheduled time to a prediction row.

    Returns (join_clause, scheduled_expr). Uses the analysis layer when it exists;
    otherwise falls back to the feed's own scheduled_time (coarser: repeats the
    previous timepoint at non-timepoints).
    """
    if marts_ready():
        return (
            f"left join intermediate.int_scheduled_stop_events se on se.trip_id = {pred_alias}.trip_id "
            f"and se.service_date = {pred_alias}.start_date and se.stop_sequence = {pred_alias}.stop_sequence",
            f"coalesce(se.scheduled_arrival, {pred_alias}.scheduled_time)",
        )
    return "", f"{pred_alias}.scheduled_time"


NEXT_STOP_CLEARANCE_M = 25  # a stop the bus is within this far of counts as reached


def next_stops(buses: pd.DataFrame, marker: int | None = None) -> pd.DataFrame:
    """Where each bus is going next, from where it actually is: its GPS position is placed on
    its trip's route shape, and the next two stops are the first two scheduled stops ahead of
    it along the shape (a stop within NEXT_STOP_CLEARANCE_M counts as reached). LTD's own
    "next stop" can be a stop the bus has just passed (LTD keeps a time for stops already
    served), so it is used only as a hint for which pass of a street the bus is on: the bus is
    placed between two stops before and two stops after LTD's next stop.

    buses: vehicle_id, trip_id, start_date, lat, lon, next_seq (LTD's hint, may be missing).
    Returns one row per bus that could be placed: vehicle_id, bus_frac, ltd_seq, and for the
    next stop and the one after (suffixes 1 and 2): seq, stop_id, stop name, lat, lon, time
    (LTD's predicted arrival, else the timetable's), time_src ('LTD' or 'timetable'), and the
    route's path from the bus to stop 1 and from stop 1 to stop 2 (GeoJSON, WGS84)."""
    rows = buses.dropna(subset=["trip_id", "lat", "lon"])
    if rows.empty or not marts_ready():
        return pd.DataFrame()
    fv = current_fv()
    sql = f"""
        with v as (
            select * from unnest(%s::text[], %s::text[], %s::date[], %s::float8[], %s::float8[],
                                 %s::int[])
                as v(vehicle_id, trip_id, start_date, lon, lat, hint_seq)
        ),
        s as (  -- the trip's stops in order, with where each lies along the shape
            select f.trip_id, f.stop_sequence, f.stop_id, f.frac,
                   row_number() over (partition by f.trip_id order by f.stop_sequence) as k
            from intermediate.int_stop_shape_fractions f
            where f.feed_version_id = {fv} and f.is_usable
              and f.trip_id in (select trip_id from v)
        ),
        g as (
            select v.*, l.line_m, l.length_m,
                   ST_Transform(ST_SetSRID(ST_MakePoint(v.lon, v.lat), 4326), 32610) as pt,
                   (select k from s where s.trip_id = v.trip_id
                      and s.stop_sequence >= v.hint_seq order by s.stop_sequence limit 1) as hint_k
            from v
            join gtfs.trips t on t.trip_id = v.trip_id and t.feed_version_id = {fv}
            join intermediate.int_shape_lines l
              on l.shape_id = t.shape_id and l.feed_version_id = t.feed_version_id
        ),
        w as materialized (  -- the stretch of shape to look in: two stops either side of LTD's next stop
            select g.*,
                   coalesce((select s.frac from s where s.trip_id = g.trip_id
                               and s.k = g.hint_k - 2), 0) as lo,
                   coalesce((select s.frac from s where s.trip_id = g.trip_id
                               and s.k = g.hint_k + 2), 1) as hi
            from g
        ),
        placed as materialized (
            select w.*,
                   lo + ST_LineLocatePoint(ST_LineSubstring(line_m, lo, greatest(hi, lo + 1e-6)), pt)
                        * (greatest(hi, lo + 1e-6) - lo) as bus_frac
            from w
        ),
        ahead as (
            select p.vehicle_id, p.trip_id, p.start_date, p.line_m, p.bus_frac, p.hint_seq,
                   s.stop_sequence, s.stop_id, s.frac,
                   row_number() over (partition by p.vehicle_id order by s.stop_sequence) as n
            from placed p
            join s on s.trip_id = p.trip_id
                  and s.frac > p.bus_frac + {NEXT_STOP_CLEARANCE_M} / nullif(p.length_m, 0)
        ),
        two as materialized (
            select a.*, st.stop_name, st.stop_lat, st.stop_lon,
                   coalesce(pc.arrival_time, pc.departure_time) as predicted,
                   coalesce(se.scheduled_arrival, pc.scheduled_time) as scheduled
            from ahead a
            join gtfs.stops st on st.stop_id = a.stop_id and st.feed_version_id = {fv}
            left join rt.prediction_current pc
              on pc.trip_id = a.trip_id and pc.start_date = a.start_date
             and pc.stop_sequence = a.stop_sequence
            left join intermediate.int_scheduled_stop_events se
              on se.trip_id = a.trip_id and se.service_date = a.start_date
             and se.stop_sequence = a.stop_sequence
            where a.n <= 2
        )
        select a.vehicle_id, a.bus_frac, a.hint_seq as ltd_seq,
               a.stop_sequence as seq1, a.stop_id as stop_id1, a.stop_name as name1,
               a.stop_lat as lat1, a.stop_lon as lon1,
               coalesce(a.predicted, a.scheduled) as time1,
               case when a.predicted is not null then 'LTD' else 'timetable' end as src1,
               b.stop_sequence as seq2, b.stop_id as stop_id2, b.stop_name as name2,
               b.stop_lat as lat2, b.stop_lon as lon2,
               coalesce(b.predicted, b.scheduled) as time2,
               case when b.predicted is not null then 'LTD' else 'timetable' end as src2,
               ST_AsGeoJSON(ST_Transform(ST_LineSubstring(a.line_m, a.bus_frac, a.frac), 4326))
                   as path1,
               case when b.frac is not null then ST_AsGeoJSON(ST_Transform(
                   ST_LineSubstring(a.line_m, a.frac, b.frac), 4326)) end as path2
        from two a
        left join two b on b.vehicle_id = a.vehicle_id and b.n = 2
        where a.n = 1
    """
    params = (
        rows["vehicle_id"].astype(str).tolist(),
        rows["trip_id"].astype(str).tolist(),
        [None if pd.isna(d) else pd.Timestamp(d).date() for d in rows["start_date"]],
        rows["lon"].astype(float).tolist(),
        rows["lat"].astype(float).tolist(),
        [int(x) if pd.notna(x) else None for x in rows["next_seq"]],
    )
    try:
        return q_fresh(sql, params, marker=marker)
    except Exception:  # noqa: BLE001 — the analysis tables may be rebuilding
        return pd.DataFrame()


def arrow_safe(df: pd.DataFrame) -> pd.DataFrame:
    """Streamlit sends tables to the browser via Arrow, which needs one type per column.
    Raw tables can hold mixed values (e.g. jsonb, ints next to strings); stringify those.
    Columns holding a single type (all text, or all clock times) are left alone."""
    out = df.copy()
    for c in out.columns:
        if out[c].dtype == object:
            kinds = {type(v) for v in out[c] if v is not None and not pd.isna(v)}
            if len(kinds) <= 1 and kinds <= {str, dtime, date}:
                continue
            out[c] = out[c].map(lambda v: None if v is None else str(v))
    return out


def table(df: pd.DataFrame, **kwargs) -> None:
    """st.dataframe with mixed-type columns made safe for the browser transfer."""
    st.dataframe(arrow_safe(df), **kwargs)


# ------------------------------------------------------------ table columns ----
def local_times(s: pd.Series) -> pd.Series:
    """Timestamps as timezone-aware Eugene times, for time columns."""
    return pd.to_datetime(s, utc=True).dt.tz_convert(LOCAL_TZ)


def late_minutes(seconds: pd.Series) -> pd.Series:
    """Seconds behind schedule -> minutes (negative = early)."""
    return pd.to_numeric(seconds, errors="coerce") / 60


def share_pct(num, den) -> pd.Series:
    """Element-wise num/den as a percentage 0-100 (missing where den is 0)."""
    num = pd.to_numeric(pd.Series(num), errors="coerce").reset_index(drop=True)
    den = pd.to_numeric(pd.Series(den), errors="coerce").reset_index(drop=True)
    return (100 * num / den.where(den > 0)).astype(float)


def hour_time(h) -> dtime | None:
    """Hour of day 0-23 -> a clock time, so an hour column sorts 5 am before 5 pm."""
    return None if h is None or pd.isna(h) else dtime(int(h) % 24)


def col_time(label: str, help: str | None = None):
    return st.column_config.DatetimeColumn(label, format="h:mm a", timezone=LOCAL_TZ, help=help)


def col_ago(label: str = "Ago"):
    return st.column_config.DatetimeColumn(label, format="distance", timezone=LOCAL_TZ)


def col_date(label: str, help: str | None = None):
    return st.column_config.DateColumn(label, format="MMM D, YYYY", help=help)


def col_hour(label: str = "Hour", help: str | None = None):
    return st.column_config.TimeColumn(label, format="h a", help=help)


def col_late(label: str = "Min late (vs timetable)", help: str | None = None):
    return st.column_config.NumberColumn(
        label,
        format="%.1f",
        help=help or "Minutes behind the printed timetable; negative = early.",
    )


def col_minutes(label: str, fmt: str = "%.0f min", help: str | None = None):
    return st.column_config.NumberColumn(label, format=fmt, help=help)


def col_pct(label: str, help: str | None = None, bar: bool = False):
    if bar:
        return st.column_config.ProgressColumn(
            label, format="%.0f%%", min_value=0, max_value=100, help=help
        )
    return st.column_config.NumberColumn(label, format="%.0f%%", help=help)


def col_count(label: str, help: str | None = None):
    return st.column_config.NumberColumn(label, format="localized", help=help)


def route_colors() -> dict[str, str]:
    routes = q("select distinct route_id from gtfs.routes order by route_id")
    return {r: ROUTE_PALETTE[i % len(ROUTE_PALETTE)] for i, r in enumerate(routes["route_id"])}


def collection_start() -> date | None:
    df = q("select min(fetched_at at time zone %s)::date as d from rt.fetch", (LOCAL_TZ,))
    return df["d"][0]


# -------------------------------------------------------------- formatting ----


def fmt_dt(ts) -> str:
    """'Sep 18, 2:25 pm'. Accepts tz-aware or naive-local timestamps; blank for missing."""
    if ts is None or (isinstance(ts, float) and math.isnan(ts)) or pd.isna(ts):
        return ""
    ts = pd.Timestamp(ts)
    if ts.tzinfo is not None:
        ts = ts.tz_convert(LOCAL_TZ)
    return ts.strftime("%b %-d, %-I:%M %p").replace("AM", "am").replace("PM", "pm")


def fmt_time(ts) -> str:
    """'2:25 pm'."""
    if ts is None or pd.isna(ts):
        return ""
    ts = pd.Timestamp(ts)
    if ts.tzinfo is not None:
        ts = ts.tz_convert(LOCAL_TZ)
    return ts.strftime("%-I:%M %p").replace("AM", "am").replace("PM", "pm")


def fmt_date(d) -> str:
    """'Sep 18' or 'Sep 18, 2026' if not this year."""
    if d is None or pd.isna(d):
        return ""
    d = pd.Timestamp(d)
    return d.strftime("%b %-d") if d.year == local_today().year else d.strftime("%b %-d, %Y")


def fmt_ago(ts, now=None) -> str:
    """'3 min ago'. Live views pass now=data_now() so the text changes only with new data."""
    if ts is None or pd.isna(ts):
        return "never"
    ts = pd.Timestamp(ts)
    if now is None:
        now = pd.Timestamp.now(tz=ts.tzinfo) if ts.tzinfo else pd.Timestamp.now()
    else:
        now = pd.Timestamp(now)
    s = int((now - ts).total_seconds())
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{s // 60} min ago"
    if s < 86400:
        return f"{s // 3600} h ago"
    return f"{s // 86400} d ago"


def fmt_delay(seconds) -> str:
    """'0.3 min late', '2.0 min early', '12 min late': tenths of a minute under 10 minutes,
    whole minutes above; 'on time' only when it rounds to 0.0."""
    if seconds is None or pd.isna(seconds):
        return "—"
    s = float(seconds)
    m = abs(s) / 60
    if round(m, 1) == 0:
        return "on time"
    txt = f"{m:.1f}" if m < 9.95 else f"{m:.0f}"
    return f"{txt} min {'late' if s > 0 else 'early'}"


def fmt_minutes(seconds) -> str:
    if seconds is None or pd.isna(seconds):
        return "—"
    m = float(seconds) / 60
    return f"{m:.1f} min".replace(".0 min", " min")


def fmt_pct(num, den) -> str:
    if not den:
        return "—"
    return f"{100 * num / den:.0f}%"


def hour_label(h: int) -> str:
    """0 -> '12 am', 13 -> '1 pm'."""
    h = int(h) % 24
    suffix = "am" if h < 12 else "pm"
    return f"{h % 12 or 12} {suffix}"


def status_from_delay(delay_s) -> str:
    if delay_s is None or pd.isna(delay_s):
        return "unknown"
    d = float(delay_s)
    if d < -60:
        return "early"
    if d > 600:
        return "very_late"
    if d > 300:
        return "late"
    return "on_time"


def weekday_type_label(w: str) -> str:
    return {"weekday": "Weekdays", "saturday": "Saturdays", "sunday": "Sundays"}.get(w, w)


# ----------------------------------------------------------------- filters ----


WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri"]


PERIODS = {"7d": "Last 7 days", "30d": "Last 30 days", "all": "All data"}
DAY_CHOICES = {
    "all": "All days",
    "weekday": "Weekdays",
    "saturday": "Saturdays",
    "sunday": "Sundays",
}


def _remembered(key: str, param: str, options: list[str], default: str) -> None:
    """Start a filter widget at the visitor's last choice on any page (kept in the session), or
    at the page address's choice (?period=30d, from a link or a bookmark), else the default."""
    if st.session_state.get(key) in options:
        return
    for v in (st.session_state.get(f"_{key}"), st.query_params.get(param)):
        if v in options:
            st.session_state[key] = v
            return
    st.session_state[key] = default


def _keep_one(key: str) -> None:
    """A segmented control clicked on its selected option clears it; keep the last choice."""
    if st.session_state.get(key) is None:
        st.session_state[key] = st.session_state.get(f"_{key}")


def page_filters() -> tuple[date, str | None]:
    """Compact filter rows under the page title. Returns (start_date, day filter), where the
    day filter is None, 'weekday', 'saturday', 'sunday', or 'dow:N' for one weekday (1 = Monday).
    Turn it into SQL with day_sql(). The choice follows the visitor from page to page and is
    kept in the page's address, so a shared or bookmarked link opens with it."""
    c1, c2 = st.columns([1, 1])
    # the default is the last 30 days: long enough that one odd day doesn't dominate, recent
    # enough to follow timetable changes
    DEFAULT_PERIOD = "30d"
    _remembered("f_period", "period", list(PERIODS), DEFAULT_PERIOD)
    period = (
        c1.segmented_control(
            "Period",
            list(PERIODS),
            format_func=PERIODS.get,
            key="f_period",
            on_change=_keep_one,
            args=("f_period",),
        )
        or DEFAULT_PERIOD
    )
    # "last 7 days" is today and the 6 days before it, as in marts.mart_lateness
    start = (
        {
            "7d": local_today() - timedelta(days=6),
            "30d": local_today() - timedelta(days=29),
        }.get(period)
        or collection_start()
        or date(2000, 1, 1)
    )
    _remembered("f_days", "days", list(DAY_CHOICES), "all")
    day = (
        c2.segmented_control(
            "Days",
            list(DAY_CHOICES),
            format_func=DAY_CHOICES.get,
            key="f_days",
            on_change=_keep_one,
            args=("f_days",),
        )
        or "all"
    )
    wt = None if day == "all" else day
    one = None
    if wt == "weekday":
        dows = ["all", *WEEKDAYS]
        _remembered("f_dow", "weekday", dows, "all")
        one = c2.segmented_control(
            "Which weekday",
            dows,
            format_func=lambda d: "All weekdays" if d == "all" else d,
            key="f_dow",
            on_change=_keep_one,
            args=("f_dow",),
        )
        if one in WEEKDAYS:
            wt = f"dow:{WEEKDAYS.index(one) + 1}"
    # remember the choice for the next page, and show it in the address
    st.session_state["_f_period"], st.session_state["_f_days"] = period, day
    st.session_state["_f_dow"] = one or "all"
    for param, value, default in (
        ("period", period, DEFAULT_PERIOD),
        ("days", day, "all"),
        ("weekday", one if wt and wt.startswith("dow:") else None, None),
    ):
        if value and value != default:
            st.query_params[param] = value
        else:
            st.query_params.pop(param, None)
    return start, wt


def filter_query() -> str:
    """The current period/days choice as a query string ('period=30d&days=weekday'), for links
    that open another page with the same filters. Empty when everything is at its default."""
    parts = []
    period = st.session_state.get("_f_period", "30d")
    day = st.session_state.get("_f_days", "all")
    one = st.session_state.get("_f_dow", "all")
    if period != "30d":
        parts.append(f"period={period}")
    if day != "all":
        parts.append(f"days={day}")
        if day == "weekday" and one in WEEKDAYS:
            parts.append(f"weekday={one}")
    return "&".join(parts)


def day_sql(
    wt: str | None, date_col: str = "service_date", type_col: str = "weekday_type"
) -> tuple[str, tuple]:
    """SQL fragment ('and ...') and parameters for a page_filters() day filter."""
    if not wt:
        return "", ()
    if wt.startswith("dow:"):
        return f"and extract(isodow from {date_col}) = %s", (int(wt[4:]),)
    return f"and {type_col} = %s", (wt,)


def day_label(wt: str | None) -> str:
    if not wt:
        return "all days"
    if wt.startswith("dow:"):
        return ["Mondays", "Tuesdays", "Wednesdays", "Thursdays", "Fridays"][int(wt[4:]) - 1]
    return weekday_type_label(wt).lower()


HEADSIGN_WORDS = {
    "SPFLD": "Springfield",
    "STA": "Station",
    "EUG": "Eugene",
    "MKTPL": "Marketplace",
    "CTR": "Center",
    "BR": "Bridge",
    "RD": "Rd",
    "TO": "to",
    "VIA": "via",
    "AND": "and",
    "AT": "at",
}
HEADSIGN_KEEP = {"UO", "LCC", "VRC", "EmX", "EMX"}


def _headsign_word(w: str) -> str:
    if "/" in w:  # "LOWELL/LCC", "DOWNTOWN LOOP/ CAMPBELL"
        return "/".join(_headsign_word(part) for part in w.split("/"))
    key = w.upper()
    if key in HEADSIGN_WORDS:
        return HEADSIGN_WORDS[key]
    if w in HEADSIGN_KEEP or key == "EMX":
        return "EmX" if key == "EMX" else w
    if w.isupper() or (w[:1].isdigit() and w[1:].isupper()):  # "THURSTON", "5TH", "11TH"
        return w.capitalize() if w[:1].isalpha() else w.lower()
    return w


def clean_headsign(headsign, route_short=None) -> str:
    if headsign is None or (isinstance(headsign, float) and math.isnan(headsign)):
        return ""
    # "<>" joins the two halves of the sign; kept as "→" unless a "to"/"via" already joins them
    text = str(headsign).replace("< >", " → ").replace("<>", " → ").replace("/ ", "/")
    words = [w for w in text.split() if w not in ("<", ">")]
    route = None if route_short is None or pd.isna(route_short) else str(route_short).lower()

    def same_route(w: str) -> bool:
        w = w.lower()
        return w == route or (w.isdigit() and route.isdigit() and int(w) == int(route))

    if route:
        words = [w for w in words if not same_route(w)]
        # "103 EmX WEST 11TH ...": a leading internal number on a route whose name isn't a number
        if words and words[0].isdigit() and not route.isdigit():
            words = words[1:]
    words = [_headsign_word(w) for w in words]
    # drop connecting words left dangling by the removals ("to to", leading or trailing "to")
    out: list[str] = []
    for w in words:
        if w in ("to", "via", "→") and out and out[-1] in ("to", "via", "→"):
            if out[-1] == "→":  # "→ to" / "→ via": the word says it better
                out[-1] = w
            continue
        out.append(w)
    while out and out[0] in ("to", "via", "→", "-"):
        out.pop(0)
    while out and out[-1] in ("to", "via", "→", "-"):
        out.pop()
    # a sign that says only the route's own name: show it as it is rather than nothing
    return " ".join(out) or " ".join(_headsign_word(w) for w in text.split() if w not in "<>")


def clean_headsigns(
    df: pd.DataFrame, headsign: str = "headsign", route: str = "route"
) -> pd.Series:
    return pd.Series(
        [clean_headsign(h, r) for h, r in zip(df[headsign], df[route], strict=False)],
        index=df.index,
    )


# ------------------------------------------------ precomputed lateness (mart_lateness) ----


def period_key(start: date) -> str:
    """page_filters()' period as mart_lateness names it."""
    today = local_today()
    if start == today - timedelta(days=6):
        return "7d"
    if start == today - timedelta(days=29):
        return "30d"
    return "all"


def days_key(wt: str | None) -> str:
    """page_filters()' day filter as mart_lateness names it: all, weekday, saturday, sunday, dowN."""
    if not wt:
        return "all"
    return "dow" + wt[4:] if wt.startswith("dow:") else wt


_LEVELS = {
    "overall": "route_id is null and stop_id is null and hour_local is null",
    "hour": "route_id is null and stop_id is null and hour_local is not null",
    "route": "route_id is not null and hour_local is null",
    "route_hour": "route_id is not null and hour_local is not null",
    "stop": "stop_id is not null",
}


def lateness(start: date, wt: str | None, level: str) -> pd.DataFrame:
    """Rows of marts.mart_lateness for a page's filters at one level: overall, hour, route,
    route_hour or stop. Every scored arrival at every stop, against the timetable. Empty while
    the table is being created (the first analysis build after an update)."""
    if not q("select to_regclass('marts.mart_lateness') is not null as ok")["ok"][0]:
        return pd.DataFrame()
    df = q(
        f"select * from marts.mart_lateness where period = %s and days = %s "
        f"and {_LEVELS[level]} order by route_id, stop_id, hour_local",
        (period_key(start), days_key(wt)),
    )
    for c in ("median_delay_s", "p10_delay_s", "p90_delay_s"):
        df[c] = df[c].astype(float)
    return df


def recomputing_note() -> None:
    st.info(
        "The numbers are being recomputed after an update to the site. "
        "They'll be back within about 20 minutes."
    )


def route_rank(start: date, wt: str | None) -> pd.DataFrame:
    """Every route's record for a period (all its stops): the report-card table on Routes
    and the comparison on each route's page. worst_hour / worst_delay_s: the hour of day when
    the typical bus on the route ran latest, and how late (hours with at least 10 arrivals)."""
    r = lateness(start, wt, "route")
    if r.empty:
        return r
    h = lateness(start, wt, "route_hour")
    h = h[h["n"] >= 10]
    worst = (
        h.sort_values(["route_id", "median_delay_s", "hour_local"], ascending=[True, False, True])
        .drop_duplicates("route_id")[["route_id", "hour_local", "median_delay_s"]]
        .rename(columns={"hour_local": "worst_hour", "median_delay_s": "worst_delay_s"})
    )
    out = r.rename(columns={"median_delay_s": "median_delay"}).merge(
        worst, on="route_id", how="left"
    )
    return out.sort_values("median_delay").reset_index(drop=True)


def worst_hour_label(hour, delay_s) -> str:
    """'5 pm · 4 min late': when a route's typical bus runs latest."""
    if hour is None or pd.isna(hour):
        return "—"
    return f"{hour_label(int(hour))} · {fmt_delay(delay_s)}"


def route_table(rank: pd.DataFrame, start: date, key: str) -> None:
    """Every route in one sortable, clickable table (rows from route_rank()): typical bus,
    the 8-in-10 range, the latest hour of the day, and today so far; the number of arrivals
    behind each row shows on hover."""
    routes = routes_by_service()
    long_names = dict(
        zip(routes["route_id"].astype(str), routes["route_long_name"].fillna(""), strict=False)
    )
    today_r = today_lateness(group_by_route=True)
    today_of = {
        str(r.route_id): (r.median_delay_s if r.n >= 10 else None) for r in today_r.itertuples()
    }
    lo, hi = range_scale(rank)
    rows = []
    for r in rank.itertuples():
        rid = str(r.route_id)
        today = today_of.get(rid)
        rows.append(
            {
                "href": route_link(r.route_id, r.route_short_name).split("#")[0],
                "hover": f"Route {r.route_short_name}: {int(r.n):,} arrivals measured since "
                f"{fmt_date(start)}",
                "route": r.route_short_name,
                "name": long_names.get(rid, ""),
                "typical": fmt_delay(r.median_delay),
                "typical_s": float(r.median_delay),
                **range_fields(r.p10_delay_s, r.median_delay, r.p90_delay_s, lo, hi),
                "worst": worst_hour_label(r.worst_hour, r.worst_delay_s),
                # in the order of the service day: 5 am first, after midnight last
                "worst_s": None if pd.isna(r.worst_hour) else service_hour_key(r.worst_hour),
                "today": fmt_delay(today),
                "today_s": today,
            }
        )
    link_table(
        rows,
        [
            {"key": "route", "label": "Route", "width": "3.4em", "bold": True},
            {
                "key": "name",
                "label": "Name",
                "width": "minmax(6em, 1.2fr)",
                "hide_on_phone": True,
            },
            {
                "key": "typical",
                "label": "Typical bus",
                "width": "7.6em",
                "help": TYPICAL_HELP,
                "sort": "typical_s",
                "first": "desc",
            },
            *range_columns(lo, hi),
            {
                "key": "worst",
                "label": "Latest hour",
                "width": "10.5em",
                "hide_on_phone": True,
                "help": "The hour of day when the typical bus on this route runs latest, and how "
                "late it is then (hours with at least 10 arrivals). Sorts by time of day.",
                "sort": "worst_s",
                "first": "asc",
            },
            {
                "key": "today",
                "label": "Today so far",
                "width": "7.6em",
                "hide_on_phone": True,
                "help": "The typical bus today, as of the latest update (every 15 minutes).",
                "sort": "today_s",
                "first": "desc",
            },
        ],
        max_height=640,
        key=key,
        default_sort="route:asc",
    )


# ----------------------------------------------- how far off LTD's predictions are ----

TIMETABLE_COLOUR = "#b35900"
AVERAGE_COLOUR = "#1c1c1c"

# shown in place of an hour-by-hour chart without enough data; .format(n=the chart's min_n)
NOT_ENOUGH_HOURLY = "Not enough arrivals yet for an hour-by-hour view (needs {n} in an hour)."

PREDICTION_OFF_NOTE = (
    "How many minutes, on average, the bus came from the time it was given, early or late "
    "alike; lower is better. Thick line with dots: LTD's real-time predictions (what apps like "
    "Transit show), by how many minutes away the prediction said the bus was. Thin straight line "
    "in the same colour: the printed timetable for the same bus arrivals (flat, because the "
    "timetable doesn't change as the bus gets nearer). Minutes away is what the prediction said "
    "at the time, not when the bus actually came. Switch lines on and off above the chart; "
    "each adds its two. Measured on bus arrivals that had a prediction 15 minutes out, each counted "
    "once at each distance; points with fewer than 20 are left out."
)

# the key for "every route together" in prediction_off() and countdown_off_chart()
ALL_ROUTES = ""


def prediction_off(start: date, wt: str | None) -> pd.DataFrame:
    """Average minutes off for the prediction at each distance (basis 'sign', ahead_min 1-15)
    and for the timetable (basis 'timetable'), all stops, every route together (route_id null)
    and each route, for the page's period and days (marts.mart_prediction_daily). Empty while
    the analysis is being rebuilt."""
    clause, params = day_sql(wt)
    try:
        return q(
            f"""
            select basis, ahead_min,
                   case when grouping(route_id) = 0 then route_id end as route_id,
                   sum(n) as n, sum(sum_abs_s)::float8 / nullif(sum(n), 0) as mean_abs_s
            from marts.mart_prediction_daily
            where service_date >= %s {clause}
            group by grouping sets ((basis, ahead_min), (basis, ahead_min, route_id))
            """,
            (start, *params),
        )
    except Exception:  # noqa: BLE001 — the mart is being rebuilt
        return pd.DataFrame()


def _prediction_stop_rows(
    where: str, params: tuple, key: str, keys: list[str] | None, start: date, wt: str | None
) -> pd.DataFrame:
    """prediction_off()'s rows from marts.mart_prediction_stop_daily for the rows matching
    `where`: everything together (route_id null) and each value of `key` (in the route_id
    column, so countdown_off_chart draws it), only those in `keys` when given. Empty while the
    analysis is being rebuilt."""
    clause, wt_params = day_sql(wt)
    only = "" if keys is None else f"having grouping({key}) = 1 or {key} = any(%s)"
    try:
        return q(
            f"""
            select basis, ahead_min, case when grouping({key}) = 0 then {key} end as route_id,
                   sum(n) as n, sum(sum_abs_s)::float8 / nullif(sum(n), 0) as mean_abs_s
            from marts.mart_prediction_stop_daily
            where {where} and service_date >= %s {clause}
            group by grouping sets ((basis, ahead_min), (basis, ahead_min, {key}))
            {only}
            """,
            (*params, start, *wt_params, *(() if keys is None else (list(keys) or ["-"],))),
        )
    except Exception:  # noqa: BLE001 — the mart is being rebuilt (or not built yet)
        return pd.DataFrame()


def countdown_off_at_stop(stop_id: str, start: date, wt: str | None) -> pd.DataFrame:
    """prediction_off() for one stop: all its routes together and each route, for the page's
    period and days."""
    return _prediction_stop_rows("stop_id = %s", (stop_id,), "route_id", None, start, wt)


def countdown_off_on_route(
    route_id: str, direction: int | None, start: date, wt: str | None, stop_ids: list[str]
) -> pd.DataFrame:
    """prediction_off() along one route (in one direction): every stop together and each stop
    asked for (its stop_id in the route_id column), for the page's period and days."""
    if direction is None:
        return _prediction_stop_rows("route_id = %s", (route_id,), "stop_id", stop_ids, start, wt)
    return _prediction_stop_rows(
        "route_id = %s and direction_id = %s", (route_id, direction), "stop_id", stop_ids, start, wt
    )


def busiest_route(df: pd.DataFrame) -> str | None:
    """The route with the most bus arrivals measured in a prediction_off() result."""
    tt = df[(df["basis"] == "timetable") & df["route_id"].notna()]
    return None if tt.empty else str(tt.sort_values("n").iloc[-1]["route_id"])


def route_toggles(
    route_ids: list[str],
    names: dict[str, str],
    default: list[str],
    key: str,
    all_label: str = "All routes",
) -> list[str]:
    """Buttons to switch lines on and off: every route together (ALL_ROUTES) first, then each
    route by number; any number at once. Returns the keys switched on."""
    ids = sorted(route_ids, key=lambda r: (len(names.get(r, r)), names.get(r, r)))
    options = [ALL_ROUTES, *ids]
    if key not in st.session_state:
        st.session_state[key] = [k for k in default if k in options] or [ALL_ROUTES]
    with st.container(key=f"rp_{key}"):
        picked = st.pills(
            "Routes",
            options,
            selection_mode="multi",
            format_func=lambda r: all_label if r == ALL_ROUTES else names.get(r, r),
            key=key,
            label_visibility="collapsed",
        )
    return [k for k in options if k in (picked or [])]


def countdown_off_chart(
    df: pd.DataFrame,
    names: dict[str, str],
    selected: list[str],
    all_label: str = "All routes",
    min_n: int = 20,
    colors: dict[str, str] | None = None,
) -> go.Figure | None:
    """Average minutes off against how many minutes away the prediction said the bus was. For
    each selected key (ALL_ROUTES or a route id) two lines in one colour: the prediction (thick,
    with dots) and the printed timetable for the same arrivals (thin, straight). None when
    nothing selected has two points to draw."""
    if df.empty or not selected:
        return None
    df = df.assign(
        _r=df["route_id"].astype(object).where(df["route_id"].notna(), ALL_ROUTES).astype(str),
        off=df["mean_abs_s"].astype(float) / 60,
    )
    sign = df[(df["basis"] == "sign") & df["ahead_min"].between(1, 15) & (df["n"] >= min_n)]
    sign = sign.sort_values("ahead_min")
    tt_rows = df[(df["basis"] == "timetable") & (df["n"] >= min_n)]
    tt = dict(zip(tt_rows["_r"], tt_rows["off"], strict=False))
    colors = route_colors() if colors is None else colors
    fig = go.Figure()
    drawn = 0
    for rank, key in enumerate(selected):
        g = sign[sign["_r"] == key]
        if len(g) < 2:
            continue
        drawn += 1
        label = all_label if key == ALL_ROUTES else names.get(key, key)
        colour = AVERAGE_COLOUR if key == ALL_ROUTES else colors.get(key, "#777777")
        t = tt.get(key)
        fig.add_trace(
            go.Scatter(
                x=g["ahead_min"],
                y=g["off"],
                mode="lines+markers",
                name=f"Predictions ({in_parens(label)})",
                legendgroup=key or "all",
                legendrank=2 * rank,
                line={"color": colour, "width": LINE_MAIN},
                marker={"size": MARKER_MAIN},
                hovertext=[
                    f"<b>{label}</b><br>predicted {int(m)} min away: {o:.1f} min off on average"
                    + (f"<br>printed timetable: {t:.1f} min off" if t is not None else "")
                    + f"<br>{int(n):,} bus arrivals"
                    for m, o, n in zip(g["ahead_min"], g["off"], g["n"], strict=False)
                ],
                hoverinfo="text",
            )
        )
        if t is not None:
            fig.add_trace(
                go.Scatter(
                    x=[1, 15],
                    y=[t, t],
                    mode="lines",
                    name=f"Timetable ({in_parens(label)})",
                    legendgroup=key or "all",
                    legendrank=2 * rank + 1,
                    line={"color": colour, "width": 1.6},
                    hovertemplate=f"<b>Printed timetable ({in_parens(label)})</b><br>{t:.1f} min off on "
                    "average, for the same bus arrivals<extra></extra>",
                )
            )
    if not drawn:
        return None
    fig.update_layout(
        xaxis={"title": "Minutes away", "range": [0, 15.5], "dtick": 5, "zeroline": False},
        # fitted to the lines, not from zero: the differences are what the chart is for
        yaxis={"title": "Average minutes off", "zeroline": False},
        # each choice's two lines (predictions, timetable) stacked as a pair in the legend
        legend={
            "orientation": "h",
            "yanchor": "bottom",
            "y": 1.02,
            "x": 0,
            "xanchor": "left",
            "traceorder": "grouped",
            "tracegroupgap": 18,
        },
        hovermode="closest",
        height=440,
    )
    return fig


ON_TIME_GREEN = "#0b6e4f"

# One look for every chart on the site: line weights, the grey of comparison lines, and (in
# show_chart) the text sizes and behaviour.
LINE_MAIN = 3.5  # the line a chart is about
MARKER_MAIN = 8
LINE_ROUTE = 2.5  # one of several lines of equal standing (a line per route)
LINE_REF = 2.5  # a comparison line (all routes together, the timetable)
MARKER_REF = 6
REF_GREY = "#9aa0a6"


CHART_TEXT = "#31333f"  # Streamlit's text colour, so charts read as part of the page
CHART_GRID = "#e4e8ea"


def show_chart(fig: go.Figure) -> None:
    """Draw a Plotly chart the way every chart on the site is drawn: the page's font at a
    readable size, light grid lines, the legend above the plot on a phone, no toolbar, and no
    zooming or panning by accident (dragging a finger over a chart on a phone should scroll the
    page); hovering still shows the numbers. Margins a chart sets itself are kept."""
    margin = {
        side: default
        for side, default in (("l", 10), ("r", 10), ("t", 30), ("b", 10))
        if getattr(fig.layout.margin, side) is None
    }
    fig.update_layout(
        template="plotly_white",
        font={
            "family": '"Source Sans", "Source Sans Pro", sans-serif',
            "size": 15,
            "color": CHART_TEXT,
        },
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        hoverlabel={"font": {"size": 15}},
        legend={"font": {"size": 15}},
        margin=margin,
        dragmode=False,
    )
    axis = {
        "tickfont": {"size": 14},
        "title_font": {"size": 15},
        "gridcolor": CHART_GRID,
        "automargin": True,
        "fixedrange": True,
    }
    fig.update_xaxes(**axis)
    if fig.layout.xaxis.showgrid is None:  # no vertical grid lines unless a chart asks for them
        fig.update_xaxes(showgrid=False)
    fig.update_yaxes(**axis)
    st.plotly_chart(
        fit_phone(fig),
        width="stretch",
        theme=None,
        config={"displayModeBar": False, "scrollZoom": False},
    )


def on_time_line(fig: go.Figure) -> None:
    """The 'on time' reference (0 minutes against the timetable) on a minutes axis: a solid
    green line drawn over the bands, the same green as the on-time line in tables. The axis
    labels it (minutes_axis)."""
    fig.add_hline(y=0, line_width=2, line_color=ON_TIME_GREEN, layer="above")


def minutes_axis(values, pad: float = 0.5) -> dict:
    """A minutes-against-the-timetable axis fitted to the values (and 0), labelled in words:
    'on time' in green at 0, then '2 min late', '1 min early' (no signs to decode)."""
    vals = [float(v) for v in values if v is not None and not pd.isna(v)] + [0.0]
    lo, hi = min(vals) - pad, max(vals) + pad
    span = hi - lo
    step = 1 if span <= 7 else 2 if span <= 14 else 5 if span <= 35 else 10
    ticks = list(range(math.ceil(lo / step) * step, math.floor(hi / step) * step + 1, step))

    def word(t: int) -> str:
        if t == 0:
            return f"<span style='color:{ON_TIME_GREEN}'><b>on time</b></span>"
        return f"{abs(t)} min {'late' if t > 0 else 'early'}"

    return {
        "range": [lo, hi],
        "tickvals": ticks,
        "ticktext": [word(t) for t in ticks],
        "zeroline": False,
        "title": "",
    }


def service_hour_key(h) -> int:
    """Sort key for hours of the day in service order: 4 am first, the after-midnight hours of
    late trips (12 am to 3 am) last, where they belong in a service day."""
    return (int(h) - 4) % 24


LATENESS_CHART_NOTE = (
    "Against the printed timetable, by the hour the bus was scheduled. The line is the typical "
    "bus (the median); below the green line = early. The shaded band is where 8 in 10 buses "
    "fell. Hover a point for the numbers. When each bus reached each stop is worked out from "
    "its GPS reports (see Data & methods)."
)


def in_parens(label: str) -> str:
    """A line's name as it reads inside a legend entry's brackets: "Route 11" -> "route 11",
    "All routes here" -> "all routes here"; names that start with a proper noun or a number
    ("EmX", "3. Olive & 11th") are kept as they are."""
    if label[:1].isupper() and label[1:2].islower() and label.split(" ")[0] in ("Route", "All"):
        return label[0].lower() + label[1:]
    return label


def as_name(label: str) -> str:
    """A line's name standing on its own in a legend: capitalised."""
    return label[:1].upper() + label[1:]


def lateness_chart(
    hourly: pd.DataFrame,
    label: str,
    color: str = "#1f5f9e",
    reference: pd.DataFrame | None = None,
    reference_label: str = "all routes",
    min_n: int = 10,
) -> go.Figure | None:
    """Typical minutes late by hour of day with the range 8 in 10 buses fall in, from rows with
    hour_local, n, median_delay_s, p10_delay_s, p90_delay_s (and n_days). reference, if given,
    is drawn as a grey comparison line. One line of hourly_lines_chart()."""
    return hourly_lines_chart(
        [{"label": label, "df": hourly, "color": color}],
        reference=reference,
        reference_label=reference_label,
        min_n=min_n,
    )


def hourly_lines_chart(
    lines: list[dict],
    reference: pd.DataFrame | None = None,
    reference_label: str = "all routes",
    min_n: int = 10,
) -> go.Figure | None:
    """Typical minutes late by hour of day, one line per entry of `lines` (dicts with 'label',
    'df' (rows with hour_local, n, median_delay_s, p10_delay_s, p90_delay_s, optionally
    n_days), 'color' and optionally 'dash'). With one line, the band where 8 in 10 of its buses
    fell is drawn too; with several, the lines alone, so they can be compared. `reference`
    (same columns) is drawn as a grey line with dots, only over the hours the lines cover.
    None when no line has an hour with min_n arrivals."""
    kept = []
    for line in lines:
        d = line["df"]
        d = d[d["n"] >= min_n] if len(d) else d
        if d is not None and len(d):
            kept.append({**line, "df": d})
    if not kept:
        return None
    hours = sorted({int(h) for ln in kept for h in ln["df"]["hour_local"]}, key=service_hour_key)
    x_of = {h: hour_label(h) for h in hours}
    one = len(kept) == 1
    fig = go.Figure()
    on_time_line(fig)
    values: list[float] = []
    for ln in kept:
        d = ln["df"].assign(_k=ln["df"]["hour_local"].map(service_hour_key)).sort_values("_k")
        x = [x_of[int(h)] for h in d["hour_local"]]
        if one:
            fig.add_trace(
                go.Scatter(
                    x=x + x[::-1],
                    y=list(d["p90_delay_s"] / 60) + list(d["p10_delay_s"] / 60)[::-1],
                    fill="toself",
                    fillcolor=ln["color"],
                    opacity=0.18,
                    mode="lines",
                    line_width=0,
                    hoverinfo="skip",
                    # named for what it is the spread of; listed after the line it belongs to
                    name=f"8 in 10 buses ({in_parens(ln['label'])})",
                    legendrank=2,
                )
            )
            values += list(d["p10_delay_s"] / 60) + list(d["p90_delay_s"] / 60)
        days = d["n_days"] if "n_days" in d else pd.Series([None] * len(d), index=d.index)
        hover = [
            (
                f"<b>{as_name(ln['label'])}</b>, {xx}<br>typical bus {fmt_delay(m)}"
                f"<br>8 in 10 buses: {fmt_range(lo, hi)}"
                f"<br>{int(n):,} arrivals" + (f" over {int(nd)} days" if pd.notna(nd) else "")
            )
            for xx, m, lo, hi, n, nd in zip(
                x,
                d["median_delay_s"],
                d["p10_delay_s"],
                d["p90_delay_s"],
                d["n"],
                days,
                strict=False,
            )
        ]
        fig.add_trace(
            go.Scatter(
                x=x,
                y=d["median_delay_s"] / 60,
                name=f"Typical bus ({in_parens(ln['label'])})" if one else as_name(ln["label"]),
                legendrank=1,
                mode="lines+markers",
                line={
                    "color": ln["color"],
                    "width": LINE_MAIN if one else LINE_ROUTE,
                    "dash": ln.get("dash", "solid"),
                },
                marker={"size": MARKER_MAIN if one else MARKER_REF},
                hovertext=hover,
                hoverinfo="text",
            )
        )
        values += list(d["median_delay_s"] / 60)
    if reference is not None and len(reference):
        # the comparison line only where the chosen lines have buses: the chart spans the hours
        # with data, not the whole service day
        ref = reference[(reference["n"] >= min_n) & reference["hour_local"].isin(hours)]
        ref = ref.assign(_k=ref["hour_local"].map(service_hour_key)).sort_values("_k")
        if len(ref):
            fig.add_trace(
                go.Scatter(
                    x=[x_of[int(h)] for h in ref["hour_local"]],
                    y=ref["median_delay_s"] / 60,
                    name=f"Typical bus ({in_parens(reference_label)})",
                    legendrank=3,
                    mode="lines+markers",
                    line={"color": REF_GREY, "width": LINE_REF},
                    marker={"size": MARKER_REF},
                    hovertext=[
                        f"<b>{as_name(reference_label)}</b>, {x_of[int(h)]}<br>typical bus "
                        f"{fmt_delay(v)}"
                        for h, v in zip(ref["hour_local"], ref["median_delay_s"], strict=False)
                    ],
                    hoverinfo="text",
                )
            )
            values += list(ref["median_delay_s"] / 60)
    fig.update_layout(
        xaxis={"type": "category", "categoryorder": "array", "categoryarray": list(x_of.values())},
        yaxis=minutes_axis(values),
        legend={"orientation": "h", "yanchor": "bottom", "y": 1.02, "x": 0, "xanchor": "left"},
        legend_title="",
        margin={"t": 30},
        height=400,
    )
    return fig


# the help text of the hour-by-hour charts on the report cards, where lines can be compared
LINES_HELP = (
    "Against the printed timetable, by the hour the bus was scheduled. Each line is the typical "
    "bus (the median); below the green line = early. With one line showing, the shaded band is "
    "where 8 in 10 of its buses fell. {choose} An hour needs 5 arrivals to show. Hover a point "
    "for the numbers."
)
# the most a report card's table (every route at a stop, every stop on a route) takes up before
# it scrolls within itself, so a long one doesn't push the rest of the page down
REPORT_TABLE_HEIGHT = 360
# every bus at every stop: the name of that line wherever a chart shows it
NETWORK = "all routes and stops"

HOURLY_LINES_SQL = """
    select {key} as key, hour_local, count(*) as n,
           percentile_cont(0.5) within group (order by delay_s) as median_delay_s,
           percentile_cont(0.1) within group (order by delay_s) as p10_delay_s,
           percentile_cont(0.9) within group (order by delay_s) as p90_delay_s,
           count(distinct service_date) as n_days
    from marts.fct_stop_events
    where status is not null and service_date >= %s {where}
    group by grouping sets ((hour_local), ({key}, hour_local))
"""


def hourly_by(key: str, where: str, params: tuple, start: date, wt: str | None) -> pd.DataFrame:
    """Lateness by hour from fct_stop_events for the page's period and days, for everything
    matching `where` together (key '' in the result) and for each value of `key` (a column or
    SQL expression), so a chart can draw any of them as lines."""
    clause, wt_params = day_sql(wt)
    df = q(
        HOURLY_LINES_SQL.format(key=key, where=f"{clause} {where}"),
        (start, *wt_params, *params),
    )
    df["key"] = df["key"].astype(object).where(df["key"].notna(), ALL_ROUTES).astype(str)
    for c in ("median_delay_s", "p10_delay_s", "p90_delay_s"):
        df[c] = df[c].astype(float)
    return df


def line_choice(
    options: list[str],
    labels: dict[str, str],
    default: list[str],
    key: str,
    all_label: str,
    many: bool = False,
) -> list[str]:
    """Switch lines on and off: everything together (ALL_ROUTES) first, then each option, any
    number at once. A row of buttons for a handful of options (routes at a stop); a searchable
    list to add from when there are many (stops along a route). Returns the keys switched
    on, in the order given."""
    opts = [ALL_ROUTES, *dict.fromkeys(options)]  # once each (a loop route passes a stop twice)
    if key not in st.session_state:
        st.session_state[key] = [k for k in default if k in opts] or [ALL_ROUTES]
    else:
        st.session_state[key] = [k for k in st.session_state[key] if k in opts]

    def fmt(k: str) -> str:
        return all_label if k == ALL_ROUTES else labels.get(k, k)

    if many:
        picked = st.multiselect(
            "Lines to show",
            opts,
            format_func=fmt,
            key=key,
            placeholder="Add stops to compare (type to search)",
            label_visibility="collapsed",
        )
    else:
        with st.container(key=f"rp_{key}"):
            picked = st.pills(
                "Lines to show",
                opts,
                selection_mode="multi",
                format_func=fmt,
                key=key,
                label_visibility="collapsed",
            )
    return [k for k in opts if k in (picked or [])]


def one_choice(
    options: list[str], labels: dict[str, str], key: str, all_label: str, label: str
) -> str:
    """Pick one line to show, from a list you can type into to search: everything together
    (ALL_ROUTES, the default) first, then each option. Choosing another replaces the one shown.
    Returns the key chosen."""
    opts = [ALL_ROUTES, *dict.fromkeys(options)]
    if st.session_state.get(key) not in opts:
        st.session_state[key] = ALL_ROUTES
    return st.selectbox(
        label,
        opts,
        format_func=lambda k: all_label if k == ALL_ROUTES else labels.get(k, k),
        key=key,
    )


def trend_chart(where: str, params: tuple, what: str) -> None:
    """'Is it getting better?': the typical bus over time for everything matching `where` in
    fct_stop_events (all days, all data), one point per day; per week once there are more
    than TREND_MAX_POINTS days of data, per month after that many weeks. Draws the card's
    chart or a note."""
    span = q(
        f"select min(service_date) as d0, max(service_date) as d1 from marts.fct_stop_events "
        f"where status is not null {where}",
        params,
    ).iloc[0]
    days = 0 if pd.isna(span["d0"]) else (span["d1"] - span["d0"]).days + 1
    unit = (
        "day" if days <= TREND_MAX_POINTS else "week" if days <= 7 * TREND_MAX_POINTS else "month"
    )
    trend = q(
        f"""
        select date_trunc(%s, service_date)::date as d, count(*) as n,
               percentile_cont(0.5) within group (order by delay_s) as median_delay
        from marts.fct_stop_events
        where status is not null {where}
        group by 1 having count(*) >= 5 order by 1
        """,
        (unit, *params),
    )
    if len(trend) < 2:
        st.caption(f"A trend needs at least two days with 5 or more arrivals {what}.")
        return
    trend["median_delay"] = trend["median_delay"].astype(float)
    when = {
        "day": lambda d: pd.Timestamp(d).strftime("%a %b %-d"),
        "week": lambda d: "Week of " + pd.Timestamp(d).strftime("%b %-d, %Y"),
        "month": lambda d: pd.Timestamp(d).strftime("%B %Y"),
    }[unit]
    fig = go.Figure()
    on_time_line(fig)
    fig.add_trace(
        go.Scatter(
            x=pd.to_datetime(trend["d"]),
            y=trend["median_delay"] / 60,
            mode="lines+markers",
            name="Typical bus",
            line={"color": "#1f5f9e", "width": LINE_MAIN},
            marker={"size": MARKER_MAIN},
            hovertext=[
                f"{when(d)}: typical bus {fmt_delay(m)} · {int(n):,} arrivals"
                for d, m, n in zip(trend["d"], trend["median_delay"], trend["n"], strict=False)
            ],
            hoverinfo="text",
        )
    )
    fig.update_layout(
        xaxis={"type": "date", "title": ""},
        yaxis=minutes_axis(list(trend["median_delay"] / 60)),
        showlegend=False,
        margin={"t": 20},
        height=320,
    )
    show_chart(fig)


def network_trend_card() -> None:
    """'Is it getting better?' for every route at every stop (the Routes and Stops pages)."""
    with card():
        st.subheader("Is it getting better?", help=TREND_HELP + " Every route at every stop.")
        trend_chart("", (), "")


PREDICTIONS_LINK = "More about the predictions →"


def network_predictions_card(start: date, wt: str | None, period: str, by_route: bool) -> None:
    """'How far off are the predictions?' for every route at every stop, as each report card has
    for its own route or stop: on the Routes page with a button per route to compare (as on the
    Predictions page), on the Stops page every stop together."""
    with card():
        st.subheader(
            "How far off are the predictions?",
            help=PREDICTION_OFF_NOTE + f" {period[0].upper() + period[1:]}.",
        )
        off = prediction_off(start, wt)
        if off.empty or off["n"].sum() == 0:
            st.caption("No measured predictions for this period yet.")
            return
        names = {}
        lines = [ALL_ROUTES]
        if by_route:
            r = routes_by_service()
            names = dict(
                zip(r["route_id"].astype(str), r["route_short_name"].astype(str), strict=False)
            )
            ids = sorted({str(x) for x in off["route_id"].dropna()} & set(names))
            lines = route_toggles(ids, names, [ALL_ROUTES], key="landing_pred_lines")
        fig = countdown_off_chart(
            off,
            {k: f"Route {v}" for k, v in names.items()},
            lines,
            # named as its button says on the Routes page; every route at every stop elsewhere
            all_label="All routes" if by_route else as_name(NETWORK),
            min_n=30,
        )
        if fig is None:
            st.caption("Not enough measured arrivals with a prediction yet for this choice.")
        else:
            show_chart(fig)
        st.page_link("views/accuracy.py", label=PREDICTIONS_LINK)


# One point per day in "Is it getting better?"; per week once there's more than this many days
# of data, per month once there's more than this many weeks, so the chart stays readable.
TREND_MAX_POINTS = 120
TREND_HELP = (
    "The typical bus against the timetable, all days, one point per day: per week once there "
    f"are more than {TREND_MAX_POINTS} days of data, per month after {TREND_MAX_POINTS} weeks. "
    "Points with fewer than 5 arrivals are left out. All data: this chart doesn't follow the "
    "period and days filters."
)


def fmt_range(p10, p90) -> str:
    """The range 8 in 10 buses fell in, in whole minutes against the timetable: '−1 to 6 min'
    (negative = early; no plus sign, which read as 'ahead' rather than 'late')."""
    if p10 is None or p90 is None or pd.isna(p10) or pd.isna(p90):
        return ""

    def m(v: float) -> str:
        r = round(float(v) / 60)
        return f"{r:d}".replace("-", "−")

    return f"{m(p10)} to {m(p90)} min"


RANGE_HELP = (
    "Where 8 in 10 buses fell, in minutes against the timetable (negative = early): the "
    "earliest tenth and the latest tenth of buses are outside it."
)


def col_range(label: str = "Usual range"):
    return st.column_config.TextColumn(label, help=RANGE_HELP)


# ------------------------------------------------------------- clickable tables ----

# the width of the "−1 to 5 min" numbers beside each range strip, and that plus the gap
RANGE_TEXT_PX = 92
_TABLE_CSS = """
<style>
.ebw-t { border: 1px solid #e3e6ea; border-radius: 10px; overflow: auto; font-size: 15px;
         display: flex; flex-direction: column; }
.ebw-r { display: grid; grid-template-columns: var(--cols); align-items: center;
         column-gap: 12px; padding: 7px 12px; border-bottom: 1px solid #eef0f2; color: #262730;
         text-decoration: none; flex: none; }
a.ebw-r:hover { background: #eef6f2; }
.ebw-h { position: sticky; top: 0; order: -1; background: #f6f8f9; font-weight: 600;
         align-items: end;
         font-size: 13px; color: #555; z-index: 1; }
.ebw-h label { cursor: pointer; user-select: none; }
.ebw-h label:hover { color: #0b6e4f; }
.ebw-so::after { content: ' ↕'; color: #b5b5b5; }
.ebw-sr { display: none; }
.ebw-n { text-align: right; font-variant-numeric: tabular-nums; }
.ebw-go { color: #0b6e4f; font-weight: 700; text-align: right; white-space: nowrap; }
.ebw-go-t { font-weight: 600; font-size: 13px; }
.ebw-k { font-weight: 700; }
.ebw-s { color: #666; font-size: 14px; }
.ebw-g { display: flex; align-items: center; gap: 8px; white-space: nowrap; }
/* the strip takes the column's width less the numbers beside it, so the
   heading's "on time" (ebw-ax, the same width) lines up with the green line in every row */
.ebw-x { position: relative; display: block; flex: 1 1 auto; min-width: 70px; height: 16px; }
.ebw-g > .ebw-s { flex: 0 0 RANGE_TEXT_PXpx; }
.ebw-x > span { position: absolute; display: block; }
.ebw-x0 { top: -10px; width: 2px; margin-left: -1px; height: calc(100% + 20px);
          background: #0b6e4f; }
.ebw-x1 { top: 4px; height: 8px; border-radius: 3px; background: #9cc0e0; }
.ebw-x2 { top: 0; width: 4px; height: 16px; border-radius: 1px; background: #123f6b; }
.ebw-ax { position: relative; display: block; width: calc(100% - RANGE_GAP_PXpx); height: 15px;
          margin-top: 3px;
          font-weight: 500; font-size: 11px; color: #777; }
.ebw-ax > span { position: absolute; top: 0; transform: translateX(-50%); white-space: nowrap; }
.ebw-ax > .ebw-ax0 { color: #0b6e4f; font-weight: 700; }
@media (max-width: 640px) {
  .ebw-hide { display: none; } .ebw-t { font-size: 14px; } .ebw-go-t { display: none; }
  .ebw-r { grid-template-columns: var(--cols-phone); padding: 7px 8px; column-gap: 8px; }
  .ebw-g { flex-direction: column; align-items: stretch; gap: 2px; }
  .ebw-g > .ebw-s { flex: none; }
  .ebw-ax { width: 100%; }
  .ebw-x0 { top: -4px; height: calc(100% + 6px); }  /* clear of the numbers under the strip */
}
</style>
""".replace("RANGE_TEXT_PX", str(RANGE_TEXT_PX)).replace("RANGE_GAP_PX", str(RANGE_TEXT_PX + 8))


def range_strip(p10, med, p90, lo: float, hi: float) -> str:
    """A small horizontal picture of where 8 in 10 buses fell (p10 to p90, seconds) with a tick at
    the typical bus and a green line at on time (0), on a shared scale lo..hi (minutes)."""
    if any(v is None or pd.isna(v) for v in (p10, med, p90)):
        return ""

    def x(v: float) -> float:  # percent of the strip's width
        return max(0.0, min(100.0, (v / 60 - lo) / (hi - lo) * 100))

    # plain positioned spans (st.html's sanitiser removes inline SVG), placed in percent so the
    # strip can shrink to fit a phone; the shared styles are the ebw-x classes above
    zero, a, b, m = x(0.0), x(p10), x(p90), x(med)
    return (
        "<span class='ebw-x'>"
        f"<span class='ebw-x1' style='left:{a:.1f}%;width:max(2px,{b - a:.1f}%)'></span>"
        # on time, drawn over the range so it shows where on time falls within it
        f"<span class='ebw-x0' style='left:{zero:.1f}%'></span>"
        f"<span class='ebw-x2' style='left:calc({m:.1f}% - 2px)'></span></span>"
    )


def _sort_key(v):
    """Numbers by value, text as people read route numbers (2 before 10); blanks go last."""
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return (2, 0, "")
    if isinstance(v, (int, float)):
        return (0, float(v), "")
    t = str(v)
    return (1, len(t) if t[:1].isdigit() else 99, t.lower())


def link_table(
    rows: list[dict],
    columns: list[dict],
    max_height: int = 460,
    key: str = "t",
    default_sort: str | None = None,
    go_label: str = "Report card",
) -> None:
    """A table whose every row is a link (click anywhere on it) and whose columns sort when
    their header is clicked (again to reverse), without reloading the page.

    rows: dicts with 'href' (optional; without it the row is not a link), 'hover' (optional
    text shown when the pointer rests on the row), and one entry per column key.
    columns: dicts with 'key', 'label', 'width' (CSS grid track), and optionally 'num'
    (right-aligned), 'bold', 'hide_on_phone', 'phone_width', 'html' (value is HTML), 'help'
    (header tooltip), 'sort' (the row entry to sort by, if not the shown value; None in the
    column dict means the column doesn't sort) and 'first' ('asc' or 'desc': the direction of
    the first click; numbers default to latest/largest first).
    key: unique per page, so two tables' sort buttons don't interfere. Links carry the page's
    period and days filters along, so the next page opens with the same choice."""
    # rows that open another page end in "Report card →" (go_label), in green like the site's
    # other links to a page; on a phone just the arrow
    linked = any(r.get("href") for r in rows)
    tracks = " ".join(c["width"] for c in columns) + (" max-content" if linked else "")
    # on a phone the hidden columns get no track; flexible columns may shrink to their content
    phone = " ".join(
        c.get("phone_width", re.sub(r"minmax\([^,]+,", "minmax(min-content,", c["width"]))
        for c in columns
        if not c.get("hide_on_phone")
    ) + (" 1.1em" if linked else "")
    go_cell = f"<div class='ebw-go'><span class='ebw-go-t'>{html_escape(go_label)} </span>→</div>"
    tid = "ebw-" + re.sub(r"[^a-z0-9]", "", key.lower())
    qs = filter_query()

    def cell(c: dict, value) -> str:
        cls = []
        if c.get("num"):
            cls.append("ebw-n")
        if c.get("bold"):
            cls.append("ebw-k")
        if c.get("hide_on_phone"):
            cls.append("ebw-hide")
        text = (
            ""
            if value is None or (not isinstance(value, str) and pd.isna(value))
            else (str(value) if c.get("html") else html_escape(str(value)))
        )
        return f"<div class='{' '.join(cls)}'>{text}</div>"

    # each sortable column: its rows' rank ascending, as a CSS variable per row
    sortable = [j for j, c in enumerate(columns) if c.get("sort", c["key"]) is not None]
    ranks: dict[int, list[int]] = {}
    for j in sortable:
        field = columns[j].get("sort", columns[j]["key"])
        order = sorted(range(len(rows)), key=lambda i, f=field: _sort_key(rows[i].get(f)))
        rank = [0] * len(rows)
        for pos, i in enumerate(order):
            rank[i] = pos
        ranks[j] = rank
    n = len(rows)

    css = []
    radios = []
    head = []
    for j, c in enumerate(columns):
        cls = ("ebw-n " if c.get("num") else "") + ("ebw-hide" if c.get("hide_on_phone") else "")
        label = html_escape(c["label"])
        tip = html_escape(c.get("help", ""))
        if j not in ranks:
            head.append(f"<div class='{cls}' title='{tip}'>{label}{c.get('axis', '')}</div>")
            continue
        first = c.get("first", "desc" if c.get("num") or c.get("html") else "asc")
        other = "asc" if first == "desc" else "desc"
        rid = f"{tid}-{j}"
        for d in ("asc", "desc"):
            checked = " checked" if default_sort == f"{c['key']}:{d}" else ""
            radios.append(
                f"<input type='radio' class='ebw-sr' name='{tid}' id='{rid}-{d}'{checked}>"
            )
            var = f"--{'a' if d == 'asc' else 'd'}{j}"
            arrow = "▲" if d == "asc" else "▼"
            css.append(
                f"#{rid}-{d}:checked ~ .ebw-t .ebw-r:not(.ebw-h) {{ order: var({var}); }}"
                f"#{rid}-{d}:checked ~ .ebw-t .{rid}-h::after "
                f"{{ content: ' {arrow}'; color: #0b6e4f; }}"
            )
        css.append(
            f".{rid}-2 {{ display: none; }}"
            f"#{rid}-{first}:checked ~ .ebw-t .{rid}-1 {{ display: none; }}"
            f"#{rid}-{first}:checked ~ .ebw-t .{rid}-2 {{ display: inline; }}"
        )
        head.append(
            f"<div class='{cls}' title='{tip}'><span class='ebw-so {rid}-h'>"
            f"<label for='{rid}-{first}' class='{rid}-1'>{label}</label>"
            f"<label for='{rid}-{other}' class='{rid}-2'>{label}</label></span>"
            f"{c.get('axis', '')}</div>"
        )

    def row(i: int, r: dict) -> str:
        style = []  # the column widths come from the table (CSS variables are inherited)
        for j, rank in ranks.items():
            style.append(f"--a{j}:{rank[i]};--d{j}:{n - 1 - rank[i]}")
        tip = f" title='{html_escape(r['hover'])}'" if r.get("hover") else ""
        body = "".join(cell(c, r.get(c["key"])) for c in columns)
        if linked:
            body += go_cell if r.get("href") else "<div></div>"
        if r.get("href"):
            href = r["href"] + (("&" if "?" in r["href"] else "?") + qs if qs else "")
            return (
                f"<a class='ebw-r' href='{html_escape(href)}' target='_self'{tip} "
                f"style='{';'.join(style)}'>{body}</a>"
            )
        return f"<div class='ebw-r'{tip} style='{';'.join(style)}'>{body}</div>"

    st.html(
        _TABLE_CSS
        + "<style>"
        + "".join(css)
        + "</style>"
        + "".join(radios)
        + f"<div class='ebw-t' style='max-height:{max_height}px;--cols:{tracks};"
        + f"--cols-phone:{phone}'>"
        + "<div class='ebw-r ebw-h'>"
        + "".join(head)
        + ("<div></div>" if linked else "")
        + "</div>"
        + "".join(row(i, r) for i, r in enumerate(rows))
        + "</div>"
    )


def range_cell(p10, med, p90, lo: float, hi: float) -> str:
    """The 'usual range' cell of a link_table: the strip and the numbers."""
    return (
        f"<span class='ebw-g'>{range_strip(p10, med, p90, lo, hi)}"
        f"<span class='ebw-s'>{html_escape(fmt_range(p10, p90))}</span></span>"
    )


def range_scale(df: pd.DataFrame, lo_col: str = "p10_delay_s", hi_col: str = "p90_delay_s"):
    """A shared minutes scale for a table's range strips: whole minutes around the data, 0 in.
    With many rows, the scale covers the earliest and latest 5% of rows' ends only, so a few
    extreme stops don't squeeze everyone else's strip into a sliver; a strip beyond the scale
    runs to the column's edge (its numbers beside it give its real range)."""
    if df.empty:
        return -2.0, 8.0
    lows, highs = df[lo_col].astype(float) / 60, df[hi_col].astype(float) / 60
    if len(df) >= 20:
        low, high = float(lows.quantile(0.05)), float(highs.quantile(0.95))
    else:
        low, high = float(lows.min()), float(highs.max())
    lo = math.floor(min(0.0, low)) - 0.5
    hi = math.ceil(max(1.0, high)) + 0.5
    return lo, hi


def range_axis(lo: float, hi: float) -> str:
    """The heading over a column of range strips: just 'on time' at 0 (green, as the line
    running down through the strips); the numbers in each row give the scale."""
    x = (0 - lo) / (hi - lo) * 100
    # centred on the line, unless that would push the words past the column's edge
    shift = 0 if x < 18 else 100 if x > 82 else 50
    return (
        "<span class='ebw-ax'>"
        f"<span class='ebw-ax0' style='left:{x:.1f}%;transform:translateX(-{shift}%)'>"
        "on time</span></span>"
    )


def range_fields(p10, med, p90, lo: float, hi: float) -> dict:
    """A table row's entries for range_columns(): the strip, its sort value (the typical bus,
    so sorting lines the dark ticks up), and the spread in minutes."""
    spread = None if pd.isna(p10) or pd.isna(p90) else float(p90 - p10)
    return {
        "range": range_cell(p10, med, p90, lo, hi),
        "range_s": None if pd.isna(med) else float(med),
        "spread": "" if spread is None else f"{spread / 60:.1f} min",
        "spread_s": spread,
    }


def range_columns(lo: float, hi: float) -> list[dict]:
    """The 8-in-10 strip column (with its minutes scale in the heading) and the spread column."""
    return [
        {
            "key": "range",
            "label": "8 in 10 buses (vs timetable)",
            "axis": range_axis(lo, hi),
            "width": "minmax(16em, 3fr)",
            "phone_width": "minmax(6.8em, 1fr)",
            "html": True,
            "help": RANGE_HELP + " The green line is on time; the dark tick is the typical bus. "
            "Sorts by the typical bus.",
            "sort": "range_s",
            "first": "desc",
        },
        {
            "key": "spread",
            "label": "Spread",
            "width": "5.2em",
            "num": True,
            "hide_on_phone": True,
            "help": "How wide the 8-in-10 range is: the time to allow for the bus being early "
            "or late. Narrower = more predictable.",
            "sort": "spread_s",
            "first": "desc",
        },
    ]


# ----------------------------------------------- lateness for any route / stop choice ----

_HOURLY_SQL = """
    select hour_local, count(*) as n,
           percentile_cont(0.5) within group (order by delay_s) as median_delay_s,
           percentile_cont(0.1) within group (order by delay_s) as p10_delay_s,
           percentile_cont(0.9) within group (order by delay_s) as p90_delay_s,
           count(distinct service_date) as n_days
    from marts.fct_stop_events
    where status is not null and service_date >= %s {where}
    group by grouping sets ((), (hour_local))
"""


def selection_lateness(
    start: date, wt: str | None, route_id: str | None = None, stop_id: str | None = None
) -> tuple[pd.Series | None, pd.DataFrame]:
    """(overall row, rows by hour) of lateness against the timetable for the page's filters and
    an optional route and/or stop. Everything and single routes come from the precomputed
    table; a stop (with or without a route) is computed from that stop's arrivals."""
    if stop_id is None:
        level = ("route", "route_hour") if route_id else ("overall", "hour")
        tot = lateness(start, wt, level[0])
        hourly = lateness(start, wt, level[1])
        if route_id:
            tot = tot[tot["route_id"].astype(str) == str(route_id)]
            hourly = hourly[hourly["route_id"].astype(str) == str(route_id)]
        return (tot.iloc[0] if len(tot) else None), hourly
    clause, params = day_sql(wt)
    where = f"and stop_id = %s {clause}"
    args: tuple = (start, stop_id, *params)
    if route_id:
        where += " and route_id = %s"
        args = (*args, route_id)
    df = q(_HOURLY_SQL.format(where=where), args)
    for c in ("median_delay_s", "p10_delay_s", "p90_delay_s"):
        df[c] = df[c].astype(float)
    tot = df[df["hour_local"].isna()]
    hourly = df[df["hour_local"].notna()].astype({"hour_local": int})
    return (tot.iloc[0] if len(tot) and int(tot["n"].iloc[0]) else None), hourly


def service_today() -> date:
    """Today's service date in Eugene: trips after midnight count to the day before until 3 am."""
    return (pd.Timestamp.now(tz=LOCAL_TZ) - pd.Timedelta(hours=3)).date()


def today_lateness(group_by_route: bool = False, route_id=None, stop_id=None) -> pd.DataFrame:
    """Lateness against the timetable so far today (as of the latest analysis build), overall or
    per route, optionally for one route and/or stop."""
    where, args = "", [service_today()]
    if route_id:
        where += " and route_id = %s"
        args.append(route_id)
    if stop_id:
        where += " and stop_id = %s"
        args.append(stop_id)
    group = "route_id," if group_by_route else ""
    df = q(
        f"""
        select {group} count(*) as n,
               percentile_cont(0.5) within group (order by delay_s) as median_delay_s
        from marts.fct_stop_events
        where status is not null and service_date = %s {where}
        {"group by route_id" if group_by_route else ""}
        """,
        tuple(args),
    )
    df["median_delay_s"] = df["median_delay_s"].astype(float)
    return df


def routes_by_service() -> pd.DataFrame:
    """Every route in today's schedule, busiest first (trips scheduled), with its names."""
    fv = current_fv()
    return q(
        f"""
        select r.route_id, r.route_short_name, r.route_long_name, count(t.trip_id) as trips
        from gtfs.routes r
        left join gtfs.trips t on t.route_id = r.route_id and t.feed_version_id = r.feed_version_id
        where r.feed_version_id = {fv}
        group by 1, 2, 3 order by 4 desc, length(r.route_short_name), r.route_short_name
        """
    )


def route_picker(
    routes: pd.DataFrame, key: str, all_label: str | None = "All routes", query_param: str = "route"
) -> str | None:
    """A row of route buttons ('All routes' first, then every route by number, all alike);
    returns the chosen route_id, or None for all routes.

    `routes` needs route_id and route_short_name. The choice is kept in the page's address
    (?route=...) so it can be bookmarked and shared. No label: the buttons say what they are."""
    names = routes.sort_values(
        "route_short_name", key=lambda c: c.astype(str).map(lambda v: (len(v), v))
    )
    ids = names["route_id"].astype(str).tolist()
    label_of = dict(zip(ids, names["route_short_name"].astype(str), strict=False))
    all_key = "__all__"
    options = ([all_key] if all_label else []) + ids
    if key not in st.session_state:
        wanted = st.query_params.get(query_param)
        st.session_state[key] = wanted if wanted in ids else (all_key if all_label else ids[0])
    elif st.session_state[key] not in options:  # e.g. a route with no data in this period
        st.session_state[key] = all_key if all_label else ids[0]
    with st.container(key=f"rp_{key}"):
        choice = st.pills(
            "Route",
            options,
            format_func=lambda r: all_label if r == all_key else label_of.get(r, str(r)),
            key=key,
            required=True,  # clicking the selected button keeps it selected
            label_visibility="collapsed",
        )
    if choice in ids:
        st.query_params[query_param] = choice
        return choice
    st.query_params.pop(query_param, None)
    return None if all_label else ids[0]


BUSY_STOPS_SQL = """
    with scored as (
        select stop_id, count(*) as n from marts.fct_stop_events
        where status is not null and service_date >= current_date - 30
        group by 1
    ),
    named as (
        select s.stop_id, s.stop_code, s.stop_name, sc.n,
               row_number() over (partition by lower(trim(s.stop_name)) order by sc.n desc) as rn
        from scored sc
        join gtfs.stops s on s.stop_id = sc.stop_id and s.feed_version_id = {fv}
        where s.location_type = 0
          -- timing points where buses queue before a station, not places riders wait
          and s.stop_name not ilike '%%arrival zone%%' and s.stop_name not ilike 'approaching %%'
    )
    select stop_id, stop_code, stop_name, n from named where rn = 1 order by n desc limit {n}
"""


def busy_stops(n: int = 9) -> pd.DataFrame:
    """The n busiest well-measured stops (see BUSY_STOPS_SQL); falls back to scheduled visits."""
    fv = current_fv()
    if marts_ready():
        df = q(BUSY_STOPS_SQL.format(fv=fv, n=int(n)))
        if len(df) >= min(n, 3):  # early days: a few well-measured stops beat a full list
            return df
    return q(
        f"""
        with visits as (
            select s.stop_id, s.stop_code, s.stop_name, count(*) as n,
                   row_number() over (partition by lower(trim(s.stop_name)) order by count(*) desc) as rn
            from gtfs.stop_times st
            join gtfs.stops s on s.stop_id = st.stop_id and s.feed_version_id = st.feed_version_id
            where st.feed_version_id = {fv} and s.location_type = 0
              and s.stop_name not ilike '%%arrival zone%%' and s.stop_name not ilike 'approaching %%'
            group by 1, 2, 3
        )
        select stop_id, stop_code, stop_name, n from visits where rn = 1 order by n desc limit {int(n)}
        """
    )


def busy_stop_buttons(key_prefix: str, n: int = 12) -> str | None:
    """Buttons for the busiest stops (as on the Stops page); returns the stop_id clicked."""
    return stop_buttons(busy_stops(n), key_prefix)


def stop_buttons(df: pd.DataFrame, key_prefix: str) -> str | None:
    """Stop buttons that pick a stop on this page, drawn like the stop cards that open a stop's
    report card (stop_tiles): name and sign number on one line, the stops' green edge, four to
    a row. Returns the stop_id clicked, if any."""
    clicked = None
    with st.container(key=f"stopbtn_{key_prefix}"):  # styled in streamlit_app.py
        cols = st.columns(4)
        for i, r in enumerate(df.itertuples()):
            label = f"**{r.stop_name}**" + (f" :gray[· #{r.stop_code}]" if r.stop_code else "")
            if cols[i % 4].button(label, key=f"{key_prefix}_{r.stop_id}", width="stretch"):
                clicked = r.stop_id
                st.session_state[f"{key_prefix}_name"] = r.stop_name
    return clicked


def card():
    """A bordered section that the site's CSS draws as a white card (streamlit_app.py styles
    the st-key-card_ class). The key comes from the calling line, so it is unique and stable."""
    caller = inspect.stack()[1]
    name = re.sub(r"[^A-Za-z0-9]", "", Path(caller.filename).stem)
    return st.container(border=True, key=f"card_{name}_{caller.lineno}")


def data_note(start: date | None = None) -> None:
    """Footer: what the site is, how much data is behind it, how fresh the analysis is."""
    first = collection_start()
    line = (
        "Eugene Bus Watch measures when Lane Transit District's buses actually reach each stop, "
        "from LTD's public schedule and live bus positions. Independent; not affiliated with LTD."
    )
    if first:
        line += f" Collecting since {fmt_date(first)}."
    if first and marts_ready():
        line += " " + build_status()
    st.caption(
        line + " [How it's measured](/methods) · "
        "[Source code on GitHub](https://github.com/blemberger/eugene-bus-reliability)"
    )


def build_status() -> str:
    """'Analysis last rebuilt 4 min ago.' — or a warning if the latest build had errors."""
    b = q("""
        select finished_at, n_errors from analytics.build_log
        where finished_at is not null order by finished_at desc limit 1
    """)
    if b.empty:
        return ""
    ago = fmt_ago(b["finished_at"][0])
    errors = b["n_errors"][0]
    if errors is not None and not pd.isna(errors) and int(errors) > 0:
        return (
            f"The latest analysis build ({ago}) had {int(errors)} error(s); numbers may be stale."
        )
    return f"Analysis last rebuilt {ago}."


def now_local() -> datetime:
    return pd.Timestamp.now(tz=LOCAL_TZ).to_pydatetime()


def local_today() -> date:
    """Today's date in Eugene (the server itself runs on UTC)."""
    return now_local().date()


# ------------------------------------------------------------ next buses at a stop ----
STOP_ROWS_PER_ROUTE = 3  # buses shown per route and direction, coming and just left


def fmt_in(minutes) -> str:
    """Minutes until a bus -> 'now', '7 min', '1 h 05 min'."""
    if minutes is None or pd.isna(minutes):
        return ""
    m = max(0, round(float(minutes)))
    if m == 0:
        return "now"
    return f"{m} min" if m < 60 else f"{m // 60} h {m % 60:02d} min"


def col_clock(label: str, times: pd.Series, help: str | None = None):
    """A time column that adds the weekday when any row is not today in Eugene; wide enough
    for "Wed 12:45 pm"."""
    t = pd.to_datetime(times, utc=True).dt.tz_convert(LOCAL_TZ).dropna()
    other_day = bool((t.dt.date != local_today()).any()) if len(t) else False
    return st.column_config.DatetimeColumn(
        label,
        format="ddd h:mm a" if other_day else "h:mm a",
        timezone=LOCAL_TZ,
        help=help,
        width=120 if other_day else 90,
    )


# narrow route and destination columns in the live tables, so the times fit
COL_ROUTE_NARROW = st.column_config.TextColumn("Route", width=60)
COL_TOWARD_NARROW = st.column_config.TextColumn("Toward", width=150)


JUST_LEFT_MINUTES = 6  # "just left" at a stop: only buses gone this recently


def stop_buses(
    stop_id: str, per_route: int = STOP_ROWS_PER_ROUTE
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """The next `per_route` buses of each route and direction at a stop, however far away in
    time, and the buses that left it in the last JUST_LEFT_MINUTES. Returns (coming, left).

    coming: LTD's live prediction where it has one; beyond the trips LTD is predicting (it
    only predicts trips about to run), the timetable (source = 'timetable'). A stop counts
    as left by the same rule as the Arrivals board: the feed kept reporting it after its
    time, or the bus's next stop was still being updated after it."""
    fv = current_fv()
    sj, sched = schedule_join("p")
    timetable = ""
    last = "coalesce(se.is_last_stop, false)" if marts_ready() else "false"
    if marts_ready():
        timetable = """
            union all
            select se.trip_id, se.service_date, se.stop_sequence, se.scheduled_arrival as t,
                   se.scheduled_arrival as scheduled, 'timetable' as source
            from intermediate.int_scheduled_stop_events se
            where se.stop_id = %(stop)s
              and se.scheduled_arrival between now() and now() + interval '36 hours'
              and not se.is_last_stop
              and not exists (
                  select 1 from rt.prediction_current x
                  where x.trip_id = se.trip_id and x.start_date = se.service_date
                    and x.stop_sequence = se.stop_sequence)"""
    df = q_fresh(
        f"""
        with pred as (
            select p.trip_id, p.start_date as service_date, p.stop_sequence,
                   coalesce(p.arrival_time, p.departure_time) as t, {sched} as scheduled,
                   p.last_seen_at, nx.next_seen, {last} as is_last
            from rt.prediction_current p
            left join lateral (
                select x.last_seen_at as next_seen from rt.prediction_current x
                where x.trip_id = p.trip_id and x.start_date = p.start_date
                  and x.stop_sequence > p.stop_sequence
                order by x.stop_sequence limit 1
            ) nx on true
            {sj}
            where p.stop_id = %(stop)s and coalesce(p.arrival_time, p.departure_time) is not null
        ),
        classed as (
            select *, t < now() and (last_seen_at >= t + interval '20 seconds'
                                     or next_seen >= t + interval '20 seconds') as departed
            from pred
            where not is_last  -- a bus ending its trip here is not one to catch
        ),
        events as (
            select trip_id, service_date, stop_sequence, t, scheduled, 'left' as source
            from classed where departed and t >= now() - interval '{JUST_LEFT_MINUTES} minutes'
            union all
            select trip_id, service_date, stop_sequence, t, scheduled, 'live'
            from classed
            where not departed and last_seen_at > now() - interval '3 minutes'
              and t >= now() - interval '2 minutes'
            {timetable}
        ),
        ranked as (
            select e.*, tr.route_id, tr.direction_id, tr.trip_headsign as headsign,
                   r.route_short_name as route,
                   coalesce(stt.timepoint, case when stt.arrival_seconds is null then 0 else 1 end) = 1
                       as is_timepoint,
                   extract(epoch from e.t - e.scheduled)::int as delay_s,
                   row_number() over (
                       partition by tr.route_id, tr.direction_id, e.source = 'left'
                       order by case when e.source = 'left' then -extract(epoch from e.t)
                                     else extract(epoch from e.t) end
                   ) as n
            from events e
            join gtfs.trips tr on tr.trip_id = e.trip_id and tr.feed_version_id = {fv}
            join gtfs.routes r on r.route_id = tr.route_id and r.feed_version_id = {fv}
            left join gtfs.stop_times stt on stt.feed_version_id = {fv} and stt.trip_id = e.trip_id
                                         and stt.stop_sequence = e.stop_sequence
        )
        select * from ranked where n <= %(n)s order by t
        """,
        {"stop": stop_id, "n": per_route},
        marker=live_marker()["fid"],
    )
    df["t"] = pd.to_datetime(df["t"], utc=True)
    coming = df[df["source"] != "left"].copy()
    left = df[df["source"] == "left"].sort_values("t", ascending=False).copy()
    now = data_now()
    coming["mins"] = ((coming["t"] - now).dt.total_seconds() / 60).clip(lower=0)
    live = coming["source"] == "live"
    coming = add_honest_columns(
        coming.assign(m=coming["mins"].where(live)), "m", "route_id", "is_timepoint"
    )
    coming.loc[~live, "delay_s"] = None  # a timetable row has no prediction to be late by
    return coming, left


def show_coming(coming: pd.DataFrame, empty: str) -> None:
    """The 'coming up' table of stop_buses()."""
    if coming.empty:
        st.caption(empty_message(empty))
        return
    table(
        pd.DataFrame(
            {
                "Route": coming["route"],
                "Toward": clean_headsigns(coming),
                "Arrives": local_times(coming["t"]),
                "In": coming["mins"].map(fmt_in),
                # text, so a timetable row shows blank rather than "None"
                "Likely in": [
                    "" if v is None or pd.isna(v) else f"{float(v):.0f} min"
                    for v in coming["likely_min"]
                ],
                "80% of the time": coming["usual_range"],
                "Scheduled": local_times(coming["scheduled"]),
                "Min late": [
                    "" if v is None or pd.isna(v) else f"{float(v):.1f}"
                    for v in late_minutes(coming["delay_s"])
                ],
                "Time from": coming["source"].map(
                    {"live": "LTD prediction", "timetable": "timetable only"}
                ),
            }
        ),
        hide_index=True,
        width="stretch",
        column_config={
            "Route": COL_ROUTE_NARROW,
            "Toward": COL_TOWARD_NARROW,
            "Arrives": col_clock("Arrives", coming["t"]),
            "In": st.column_config.TextColumn(
                "In",
                help="Time until LTD's predicted time, or the timetable's where LTD has no prediction yet.",
            ),
            "Likely in": st.column_config.TextColumn("Likely in", help=col_likely()["help"]),
            "80% of the time": col_usual(),
            "Scheduled": col_clock("Scheduled", coming["scheduled"]),
            "Min late": st.column_config.TextColumn(
                "Min late",
                help="How late LTD's prediction puts the bus, in minutes; negative = early.",
            ),
            "Time from": st.column_config.TextColumn(
                "Time from",
                help="LTD only predicts trips that are about to run; later buses show the timetable.",
            ),
        },
    )


def show_left(left: pd.DataFrame, empty: str) -> None:
    """The 'just left' table of stop_buses()."""
    if left.empty:
        st.caption(empty)
        return
    table(
        pd.DataFrame(
            {
                "Route": left["route"],
                "Toward": clean_headsigns(left),
                "Left at": local_times(left["t"]),
                "Scheduled": local_times(left["scheduled"]),
                "Min late": late_minutes(left["delay_s"]),
                "Ago": local_times(left["t"]),
            }
        ),
        hide_index=True,
        width="stretch",
        column_config={
            "Route": COL_ROUTE_NARROW,
            "Toward": COL_TOWARD_NARROW,
            "Left at": col_clock("Left at", left["t"]),
            "Scheduled": col_clock("Scheduled", left["scheduled"]),
            "Min late": col_late(),
            "Ago": col_ago(),
        },
    )
