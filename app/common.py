"""Shared helpers for the dashboard: database access, formatting, filters."""

from __future__ import annotations

import math
import os
import re
import time
import uuid
from datetime import date, datetime, timedelta
from datetime import time as dtime
from html import escape as html_escape
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
STOP_NAME_COLUMNS = ("stop_name", "next_stop", "stop2")


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


def _run(sql: str, params: tuple = ()) -> pd.DataFrame:
    """Run one read-only query and return a DataFrame (no caching)."""
    t0 = time.monotonic()
    with psycopg.connect(DATABASE_URL) as conn, conn.transaction(), conn.cursor() as cur:
        cur.execute("SET TRANSACTION READ ONLY")
        cur.execute(sql, params)
        cols = [d.name for d in cur.description]
        df = readable_stop_names(pd.DataFrame(cur.fetchall(), columns=cols))
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


def q(sql: str, params: tuple = ()) -> pd.DataFrame:
    """Run a read-only query and return a DataFrame. Queries on the raw realtime tables (rt.*)
    are cached for 60 s; everything else until the next analysis build (at most an hour)."""
    if _RAW_TABLES.search(sql):
        return _q_raw(sql, params)
    return _q_built(sql, params, build_marker())


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
        label, format="%+.1f min", min_value=0.0, max_value=float(max_minutes), help=TYPICAL_HELP
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


def log_page_view(page: str) -> None:
    """Count a page view in site.page_view: the time, the page (with the stop or route
    chosen on it), phone or computer, and a random id for this browser tab's session, so
    visits can be counted. No IP address, cookie or anything identifying is stored. Logged
    once per page and choice, not on every rerun; never interrupts the page."""
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
    try:
        with psycopg.connect(DATABASE_URL, connect_timeout=3) as conn:
            conn.read_only = (
                False  # the site's login is read-only by default; this table takes inserts
            )
            conn.execute(
                "insert into site.page_view (session_id, page, detail, device) values (%s, %s, %s, %s)",
                (session_id, page, detail, device),
            )
    except Exception:  # noqa: BLE001 — a missing table or a busy database must not break the page
        pass


def fit_phone(fig):
    """On a phone, move a chart's legend from beside the plot to above it, so the plot keeps the
    screen's full width. On every screen, minute axes signed +/− label zero plain "0"."""
    for axis in ("xaxis", "yaxis"):
        fmt = getattr(fig.layout, axis).tickformat or ""
        if fmt.startswith("+"):
            fig.update_layout({axis: {"labelalias": {"+0": "0", "+0.0": "0"}}})
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


def col_date(label: str):
    return st.column_config.DateColumn(label, format="MMM D, YYYY")


def col_hour(label: str = "Hour", help: str | None = None):
    return st.column_config.TimeColumn(label, format="h a", help=help)


def col_late(label: str = "Min late (vs timetable)", help: str | None = None):
    return st.column_config.NumberColumn(
        label,
        format="%+.1f",
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


def fmt_delay(seconds, on_time_band: int = 30) -> str:
    """'4 min late', '2 min early', 'on time'. Half-minute precision under 10 minutes."""
    if seconds is None or pd.isna(seconds):
        return "—"
    s = float(seconds)
    if abs(s) < on_time_band:
        return "on time"
    m = abs(s) / 60
    txt = f"{m:.1f}".rstrip("0").rstrip(".") if m < 10 else f"{m:.0f}"
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


def page_filters() -> tuple[date, str | None]:
    """Compact filter rows under the page title. Returns (start_date, day filter), where the
    day filter is None, 'weekday', 'saturday', 'sunday', or 'dow:N' for one weekday (1 = Monday).
    Turn it into SQL with day_sql()."""
    c1, c2 = st.columns([1, 1])
    span = (
        c1.segmented_control(
            "Period", ["Last 7 days", "Last 30 days", "All data"], default="Last 7 days"
        )
        or "Last 7 days"
    )
    # "last 7 days" is today and the 6 days before it, as in marts.mart_lateness
    start = (
        {
            "Last 7 days": local_today() - timedelta(days=6),
            "Last 30 days": local_today() - timedelta(days=29),
        }.get(span)
        or collection_start()
        or date(2000, 1, 1)
    )
    day = (
        c2.segmented_control(
            "Days", ["All days", "Weekdays", "Saturdays", "Sundays"], default="All days"
        )
        or "All days"
    )
    wt = {"Weekdays": "weekday", "Saturdays": "saturday", "Sundays": "sunday"}.get(day)
    if wt == "weekday":
        one = c2.segmented_control(
            "Which weekday", ["All weekdays", *WEEKDAYS], default="All weekdays"
        )
        if one in WEEKDAYS:
            wt = f"dow:{WEEKDAYS.index(one) + 1}"
    return start, wt


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


# ------------------------------------------- what you were told vs when the bus came ----

# Minutes the countdown showed, shown left to right (far to near). The mart also has 20 and 30;
# riders decide when to leave within about 15 minutes, so the chart stops there.
TOLD_AHEAD = (15, 10, 5, 3, 2, 1)


def told_vs_actual(route_id: str | None = None, stop_id: str | None = None) -> pd.DataFrame:
    """Rows of marts.mart_told_vs_actual for all routes and stops, one route, or one stop."""
    if not q("select to_regclass('marts.mart_told_vs_actual') is not null as ok")["ok"][0]:
        return pd.DataFrame()
    df = q(
        "select * from marts.mart_told_vs_actual "
        "where route_id is not distinct from %s and stop_id is not distinct from %s",
        (route_id, stop_id),
    )
    for c in ("median_s", "p10_s", "p90_s", "median_abs_s"):
        df[c] = df[c].astype(float)
    return df


TOLD_CHART_NOTE = (
    "Blue: LTD's countdown (on signs and in apps) when it said the bus was that many minutes "
    "away. Grey: the printed timetable, the same at any distance because it never changes. Each "
    "line is how much later than that the typical bus came (below zero = earlier); each shaded "
    "band is where 8 in 10 buses fell. The narrower the band, the more you can rely on it."
)


def told_chart(df: pd.DataFrame, min_n: int = 20) -> go.Figure | None:
    """How much later than the timetable, and than the countdown N minutes out, the bus came.
    The countdown is a line from 15 minutes out to 1; the timetable a flat band across the same
    range, so the two compare directly. Countdown points with fewer than min_n predictions are
    left out (the chart only spans the distances that have data)."""
    if df.empty:
        return None
    tt = df[(df["basis"] == "timetable") & (df["n"] >= min_n)]
    sign = df[
        (df["basis"] == "sign") & (df["n"] >= min_n) & (df["ahead_min"].isin(TOLD_AHEAD))
    ].sort_values("ahead_min", ascending=False)
    if sign.empty:
        return None
    x = [f"{int(m)} min" for m in sign["ahead_min"]]

    def hover(r, what: str, unit: str) -> str:
        return (
            f"<b>{what}</b><br>typical bus {r.median_s / 60:+.1f} min vs this"
            f"<br>8 in 10 buses: {r.p10_s / 60:+.1f} to {r.p90_s / 60:+.1f} min"
            f"<br>right to within 1 min: {r.n_within_1min / r.n:.0%}"
            f" · within 2 min: {r.n_within_2min / r.n:.0%}<br>{int(r.n):,} {unit}"
        )

    fig = go.Figure()
    fig.add_hline(y=0, line_width=1, line_color="#999", line_dash="dot")
    lo, hi = float(sign["p10_s"].min()), float(sign["p90_s"].max())
    if not tt.empty:
        r = tt.iloc[0]
        lo, hi = min(lo, r.p10_s), max(hi, r.p90_s)
        fig.add_trace(
            go.Scatter(
                x=x + x[::-1],
                y=[r.p90_s / 60] * len(x) + [r.p10_s / 60] * len(x),
                fill="toself",
                fillcolor="rgba(110,110,110,0.16)",
                mode="lines",
                line_width=0,
                hoverinfo="skip",
                name="Timetable: 8 in 10 buses",
            )
        )
        fig.add_trace(
            go.Scatter(
                x=x,
                y=[r.median_s / 60] * len(x),
                mode="lines",
                name="Timetable: typical bus",
                line={"color": "#6b6b6b", "width": 2.5, "dash": "dash"},
                hovertext=[hover(r, "The printed timetable", "arrivals")] * len(x),
                hoverinfo="text",
            )
        )
    fig.add_trace(
        go.Scatter(
            x=x + x[::-1],
            y=list(sign["p90_s"] / 60) + list(sign["p10_s"] / 60)[::-1],
            fill="toself",
            fillcolor="rgba(31,95,158,0.20)",
            mode="lines",
            line_width=0,
            hoverinfo="skip",
            name="Countdown: 8 in 10 buses",
        )
    )
    fig.add_trace(
        go.Scatter(
            x=x,
            y=sign["median_s"] / 60,
            mode="lines+markers",
            name="Countdown: typical bus",
            line={"color": "#1f5f9e", "width": 3},
            hovertext=[
                hover(r, f"When the countdown said {int(r.ahead_min)} min", "predictions")
                for r in sign.itertuples()
            ],
            hoverinfo="text",
        )
    )
    pad = 0.4
    fig.update_layout(
        xaxis={
            "type": "category",
            "categoryorder": "array",
            "categoryarray": x,
            "title": "what the countdown said",
        },
        yaxis={
            "title": "minutes later than told",
            "range": [min(0.0, lo / 60) - pad, max(0.0, hi / 60) + pad],
            "tickformat": "+.0f",
            "zeroline": False,
        },
        legend={"orientation": "h", "yanchor": "bottom", "y": 1.02, "x": 0, "xanchor": "left"},
        margin={"t": 50},
        height=400,
    )
    return fig


def service_hour_key(h) -> int:
    """Sort key for hours of the day in service order: 4 am first, the after-midnight hours of
    late trips (12 am to 3 am) last, where they belong in a service day."""
    return (int(h) - 4) % 24


LATENESS_CHART_NOTE = (
    "Measured against the printed timetable. The line is the typical bus (the median) in each "
    "hour, in minutes behind the timetable; below zero = early. The shaded band is where 8 in 10 "
    "buses fell. By scheduled hour; hover a point for details."
)


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
    is drawn as a thin grey dashed line for comparison."""
    d = hourly[hourly["n"] >= min_n] if len(hourly) else hourly
    if d is None or d.empty:
        return None
    d = d.assign(_k=d["hour_local"].map(service_hour_key)).sort_values("_k")
    hours = sorted(set(d["hour_local"].astype(int)), key=service_hour_key)
    ref = None
    if reference is not None and len(reference):
        ref = reference[reference["n"] >= min_n]
        ref = ref.assign(_k=ref["hour_local"].map(service_hour_key)).sort_values("_k")
        hours = sorted(set(hours) | set(ref["hour_local"].astype(int)), key=service_hour_key)
    x_of = {h: hour_label(h) for h in hours}
    x = [x_of[int(h)] for h in d["hour_local"]]
    fig = go.Figure()
    fig.add_hline(y=0, line_width=1, line_color="#999", line_dash="dot")
    fig.add_trace(
        go.Scatter(
            x=x + x[::-1],
            y=list(d["p90_delay_s"] / 60) + list(d["p10_delay_s"] / 60)[::-1],
            fill="toself",
            fillcolor=color,
            opacity=0.18,
            mode="lines",
            line_width=0,
            hoverinfo="skip",
            name="8 in 10 buses",
        )
    )
    if ref is not None and len(ref):
        fig.add_trace(
            go.Scatter(
                x=[x_of[int(h)] for h in ref["hour_local"]],
                y=ref["median_delay_s"] / 60,
                name=f"Typical bus, {reference_label}",
                mode="lines",
                line={"color": "#888", "width": 1.5, "dash": "dash"},
                hovertemplate="%{x}: typically %{y:+.1f} min<extra>" + reference_label + "</extra>",
            )
        )
    days = d["n_days"] if "n_days" in d else pd.Series([None] * len(d), index=d.index)
    hover = [
        (
            f"<b>{label}</b>, {xx}<br>typical bus {m / 60:+.1f} min"
            f"<br>8 in 10 buses: {lo / 60:+.0f} to {hi / 60:+.0f} min"
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
            name=f"Typical bus, {label}",
            mode="lines+markers",
            line={"color": color, "width": 2.5},
            hovertext=hover,
            hoverinfo="text",
        )
    )
    values = list(d["p10_delay_s"] / 60) + list(d["p90_delay_s"] / 60) + [0.0]
    if ref is not None and len(ref):
        values += list(ref["median_delay_s"] / 60)
    fig.update_layout(
        xaxis={"type": "category", "categoryorder": "array", "categoryarray": list(x_of.values())},
        yaxis={
            "title": "minutes behind the timetable",
            "tickformat": "+d",
            "range": [min(values) - 0.5, max(values) + 0.5],
            "zeroline": False,
        },
        legend={"orientation": "h", "yanchor": "bottom", "y": 1.02, "x": 0, "xanchor": "left"},
        legend_title="",
        margin={"t": 30},
        height=380,
    )
    return fig


def fmt_range(p10, p90) -> str:
    """The range 8 in 10 buses fell in, in whole minutes against the timetable: '−1 to +6 min'."""
    if p10 is None or p90 is None or pd.isna(p10) or pd.isna(p90):
        return ""

    def m(v: float) -> str:
        r = round(float(v) / 60)
        return "0" if r == 0 else f"{r:+d}".replace("-", "−")

    return f"{m(p10)} to {m(p90)} min"


RANGE_HELP = (
    "Where 8 in 10 buses fell, in minutes against the timetable (negative = early): the "
    "earliest tenth and the latest tenth of buses are outside it."
)


def col_range(label: str = "Usual range"):
    return st.column_config.TextColumn(label, help=RANGE_HELP)


# ------------------------------------------------------------- clickable tables ----

_TABLE_CSS = """
<style>
.ebw-t { border: 1px solid #e3e6ea; border-radius: 10px; overflow: auto; font-size: 15px; }
.ebw-r { display: grid; align-items: center; column-gap: 12px; padding: 7px 12px;
         border-bottom: 1px solid #eef0f2; color: #262730; text-decoration: none; }
a.ebw-r:hover { background: #eef6f2; }
.ebw-h { position: sticky; top: 0; background: #f6f8f9; font-weight: 600; font-size: 13px;
         color: #555; z-index: 1; }
.ebw-n { text-align: right; font-variant-numeric: tabular-nums; }
.ebw-k { font-weight: 700; }
.ebw-s { color: #666; font-size: 14px; }
.ebw-g { display: flex; align-items: center; gap: 8px; white-space: nowrap; }
.ebw-r { grid-template-columns: var(--cols); }
@media (max-width: 640px) {
  .ebw-hide { display: none; } .ebw-t { font-size: 14px; }
  .ebw-r { grid-template-columns: var(--cols-phone); padding: 7px 8px; column-gap: 8px; }
  .ebw-g { flex-direction: column; align-items: stretch; gap: 2px; }
}
</style>
"""


def range_strip(p10, med, p90, lo: float, hi: float, width: int = 130) -> str:
    """A small horizontal picture of where 8 in 10 buses fell (p10 to p90, seconds) with a tick at
    the typical bus and a faint line at 0, on a shared scale lo..hi (minutes)."""
    if any(v is None or pd.isna(v) for v in (p10, med, p90)):
        return ""

    def x(v: float) -> float:  # percent of the strip's width
        return max(0.0, min(100.0, (v / 60 - lo) / (hi - lo) * 100))

    # plain positioned spans (st.html's sanitiser removes inline SVG), placed in percent so the
    # strip can shrink to fit a phone
    zero, a, b, m = x(0.0), x(p10), x(p90), x(med)
    box = "position:absolute;display:block"
    return (
        f"<span style='position:relative;display:inline-block;flex:none;width:{width}px;"
        f"max-width:100%;height:16px'>"
        f"<span style='{box};left:{zero:.1f}%;top:0;width:1px;height:16px;background:#bbb'></span>"
        f"<span style='{box};left:{a:.1f}%;top:4px;width:max(2px,{b - a:.1f}%);height:8px;"
        f"border-radius:3px;background:#9cc0e0'></span>"
        f"<span style='{box};left:calc({m:.1f}% - 1.5px);top:1px;width:3px;height:14px;"
        f"border-radius:1px;background:#1f5f9e'></span></span>"
    )


def link_table(rows: list[dict], columns: list[dict], max_height: int = 460) -> None:
    """A table whose every row is a link (click anywhere on it). rows: dicts with 'href' and one
    entry per column key; columns: dicts with 'key', 'label', 'width' (CSS grid track), and
    optionally 'num' (right-aligned), 'bold', 'hide_on_phone', 'phone_width', 'html' (value is
    HTML)."""
    tracks = " ".join(c["width"] for c in columns)
    # on a phone the hidden columns get no track; a column can name a narrower phone_width
    # (by default flexible columns may shrink as far as their content allows)
    phone = " ".join(
        c.get("phone_width", re.sub(r"minmax\([^,]+,", "minmax(min-content,", c["width"]))
        for c in columns
        if not c.get("hide_on_phone")
    )
    grid = f"--cols:{tracks};--cols-phone:{phone}"

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

    head = "".join(
        f"<div class='{'ebw-n ' if c.get('num') else ''}{'ebw-hide' if c.get('hide_on_phone') else ''}'"
        f" title='{html_escape(c.get('help', ''))}'>{html_escape(c['label'])}</div>"
        for c in columns
    )
    body = "".join(
        f"<a class='ebw-r' href='{html_escape(r['href'])}' target='_self' "
        f"style='{grid}'>" + "".join(cell(c, r.get(c["key"])) for c in columns) + "</a>"
        for r in rows
    )
    st.html(
        _TABLE_CSS + f"<div class='ebw-t' style='max-height:{max_height}px'>"
        f"<div class='ebw-r ebw-h' style='{grid}'>{head}</div>{body}</div>"
    )


def range_cell(p10, med, p90, lo: float, hi: float) -> str:
    """The 'usual range' cell of a link_table: the strip and the numbers."""
    return (
        f"<span class='ebw-g'>{range_strip(p10, med, p90, lo, hi)}"
        f"<span class='ebw-s'>{html_escape(fmt_range(p10, p90))}</span></span>"
    )


def range_scale(df: pd.DataFrame, lo_col: str = "p10_delay_s", hi_col: str = "p90_delay_s"):
    """A shared minutes scale for a table's range strips: whole minutes around the data, 0 in."""
    if df.empty:
        return -2.0, 8.0
    lo = math.floor(min(0.0, float(df[lo_col].min()) / 60)) - 0.5
    hi = math.ceil(max(1.0, float(df[hi_col].quantile(0.95)) / 60)) + 0.5
    return lo, hi


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
    """A row of route chips (one per route, plus 'All routes'); returns the chosen route_id.

    `routes` needs route_id and route_short_name. The choice is kept in the page's address
    (?route=...) so it can be bookmarked and shared."""
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
    choice = st.pills(
        "Route",
        options,
        format_func=lambda r: all_label if r == all_key else label_of.get(r, str(r)),
        key=key,
        required=True,  # clicking the selected chip keeps it selected
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


def busy_stop_buttons(key_prefix: str, n: int = 9) -> str | None:
    """Buttons for the busiest well-measured stops; returns the stop_id clicked."""
    return stop_buttons(busy_stops(n), key_prefix)


def stop_buttons(df: pd.DataFrame, key_prefix: str, show_code: bool = False) -> str | None:
    """Three columns of stop buttons; returns the stop_id clicked, if any. The sign number
    (#code) is shown only when asked (search results) or when two buttons share a name."""
    clicked = None
    cols = st.columns(3)
    dup = df["stop_name"].duplicated(keep=False)
    for i, r in enumerate(df.itertuples()):
        with_code = (show_code or dup.iloc[i]) and r.stop_code
        label = f"{r.stop_name}" + (f"  ·  #{r.stop_code}" if with_code else "")
        if cols[i % 3].button(label, key=f"{key_prefix}_{r.stop_id}", width="stretch"):
            clicked = r.stop_id
            st.session_state[f"{key_prefix}_name"] = r.stop_name
    return clicked


def data_note(start: date | None = None) -> None:
    """Footer caption: how much data is behind the numbers, and how fresh the analysis is."""
    first = collection_start()
    if not first:
        return
    line = f"Collecting since {fmt_date(first)}."
    if marts_ready():
        cov = q("""
            select count(*) as days, min(service_date) as d0, max(service_date) as d1,
                   sum(stop_events_observed) as scored
            from marts.mart_daily_coverage where stop_events_observed > 0
        """).iloc[0]
        if cov["days"]:
            line += (
                f" Scored data: {int(cov['days'])} day{'s' if int(cov['days']) != 1 else ''} "
                f"({fmt_date(cov['d0'])}–{fmt_date(cov['d1'])}), {int(cov['scored']):,} stop events."
            )
        line += " " + build_status()
    st.caption(
        line + " Typical bus = the median minutes behind the printed timetable; 8 in 10 buses = "
        "the range between the earliest and latest tenth. "
        "[Source code on GitHub](https://github.com/blemberger/eugene-bus-reliability)."
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
    """A time column that adds the weekday when any row is not today in Eugene."""
    t = pd.to_datetime(times, utc=True).dt.tz_convert(LOCAL_TZ).dropna()
    other_day = bool((t.dt.date != local_today()).any()) if len(t) else False
    return st.column_config.DatetimeColumn(
        label, format="ddd h:mm a" if other_day else "h:mm a", timezone=LOCAL_TZ, help=help
    )


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
                    "" if v is None or pd.isna(v) else f"{float(v):+.1f}"
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
            "Left at": col_clock("Left at", left["t"]),
            "Scheduled": col_clock("Scheduled", left["scheduled"]),
            "Min late": col_late(),
            "Ago": col_ago(),
        },
    )
