"""Shared helpers for the dashboard: database access, formatting, filters."""

from __future__ import annotations

import math
import os
import re
import time
from datetime import date, datetime, timedelta
from datetime import time as dtime
from urllib.parse import quote

import pandas as pd
import psycopg
import streamlit as st


def _database_url() -> str:
    return os.environ.get("DATABASE_URL", "")


DATABASE_URL = _database_url()
LOCAL_TZ = "America/Los_Angeles"
EXPLORER_MAX_ROWS = 5000

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
            parts["side"] = _COMPASS[m.group("side").upper()]
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


@st.cache_data(ttl=60)
def q(sql: str, params: tuple = ()) -> pd.DataFrame:
    """Run a read-only query and return a DataFrame. Cached for 60 s."""
    with psycopg.connect(DATABASE_URL) as conn, conn.transaction(), conn.cursor() as cur:
        cur.execute("SET TRANSACTION READ ONLY")
        cur.execute(sql, params)
        cols = [d.name for d in cur.description]
        return readable_stop_names(pd.DataFrame(cur.fetchall(), columns=cols))


@st.cache_data(ttl=10)
def q_live(sql: str, params: tuple = ()) -> pd.DataFrame:
    """Same as q() with a 10 s cache, for parts of the page that refresh themselves."""
    return q.__wrapped__(sql, params)


def run_explorer_query(sql: str, max_rows: int = EXPLORER_MAX_ROWS) -> tuple[pd.DataFrame, bool]:
    """Run one visitor-supplied SELECT safely. Returns (rows, truncated)."""
    sql = sql.strip().rstrip(";").strip()
    with psycopg.connect(DATABASE_URL) as conn:
        conn.read_only = True
        with conn.transaction():
            conn.execute("SET LOCAL statement_timeout = '15s'")
            with conn.cursor(name="explorer") as cur:
                cur.execute(sql)
                rows = cur.fetchmany(max_rows + 1)
                cols = [d.name for d in cur.description]
    return pd.DataFrame(rows[:max_rows], columns=cols), len(rows) > max_rows


# Every live view shows the data "as of" the newest message (data_now), not the wall clock,
# so a page changes only when new data arrives, at the moment the countdown resets. Pages
# check for new data every LIVE_CHECK_SECONDS; the marker itself is cached for 1 s.
POLL_SECONDS = 30
LIVE_CHECK_SECONDS = 2


@st.cache_data(ttl=1)
def live_marker() -> dict:
    """Id and arrival time of the newest realtime message stored."""
    row = q.__wrapped__(
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


@st.cache_data(ttl=600)
def q_fresh(sql: str, params: tuple = (), marker: int | None = None) -> pd.DataFrame:
    """q() for live data, re-run only when `marker` (live_marker()['fid']) changes."""
    return q.__wrapped__(sql, params)


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
    return f"""<div style="display:flex;flex-wrap:wrap;align-items:center;gap:6px 16px;margin:2px 0 6px 0;">
  <div style="font-size:22px;font-weight:700;color:{colour};min-width:10.5em;">&#9679; {headline}</div>
  <div style="flex:1 1 160px;height:10px;background:#dfece5;border-radius:5px;overflow:hidden;">
    <div style="height:100%;width:{done:.0f}%;background:{colour};"></div>
  </div>
  <div style="flex-basis:100%;font-size:14px;color:#555;">
    LTD data from {local(at)} · next update due about {local(due)}</div>
</div>"""


@st.fragment(run_every="1s")
def live_status_line() -> None:
    """A large countdown to LTD's next update, with a progress bar and the time of the data."""
    st.html(_countdown_html())


def stop_link(stop_id, name) -> str | None:
    """A link to a stop's page, for LinkColumn (the text after # is what the cell shows)."""
    if stop_id is None or pd.isna(stop_id):
        return None
    return f"/stops?stop={quote(str(stop_id))}#{'' if name is None or pd.isna(name) else name}"


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


@st.cache_data(ttl=300)
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


@st.cache_data(ttl=300)
def marts_ready() -> bool:
    df = q(
        "select count(*) as n from information_schema.tables where table_schema = 'marts' and table_name = 'fct_stop_events'"
    )
    return int(df["n"][0]) > 0


@st.cache_data(ttl=300)
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


@st.cache_data(ttl=15)
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


def refresh_countdown(seconds: int) -> None:
    """A small 'Live · next update in N s' line that counts down in the browser."""
    st.iframe(
        f"""<!-- {time.time()} --><div id='c' style='font: 14px system-ui, sans-serif; color: #666;'></div>
        <script>var n = {seconds}; var el = document.getElementById('c');
        function t() {{ el.textContent = 'Live · next update in ' + n + ' s'; if (n > 0) {{ n--; setTimeout(t, 1000); }} }} t();</script>""",
        height=26,
    )


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
            "Reliability numbers appear after the analysis layer has been built at least once: "
            "run `make dbt-build`. It derives observed arrivals from the collected positions "
            "and needs at least a few hours of collection to show anything."
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


def col_hour(label: str = "Hour"):
    return st.column_config.TimeColumn(label, format="h a")


def col_late(label: str = "Min late", help: str | None = None):
    return st.column_config.NumberColumn(
        label, format="%+.1f", help=help or "Minutes behind schedule; negative = early."
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
    start = {
        "Last 7 days": local_today() - timedelta(days=7),
        "Last 30 days": local_today() - timedelta(days=30),
    }.get(span, date(2000, 1, 1))
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
    return " ".join(out)


def clean_headsigns(
    df: pd.DataFrame, headsign: str = "headsign", route: str = "route"
) -> pd.Series:
    return pd.Series(
        [clean_headsign(h, r) for h, r in zip(df[headsign], df[route], strict=False)],
        index=df.index,
    )


def route_rank(start: date, wt: str | None) -> pd.DataFrame:
    """Every route's on-time record for a period: the report-card table on Routes and the
    comparison on each route's page. True medians, from the stop events."""
    clause, params = day_sql(wt)
    return q(
        f"""
        with ev as (
            select route_id, route_short_name, hour_local, status, delay_s
            from marts.fct_stop_events
            where status is not null and is_timepoint and service_date >= %s {clause}
        ),
        worst as (
            select distinct on (route_id) route_id, hour_local as worst_hour
            from (
                select route_id, hour_local, count(*) as n,
                       count(*) filter (where status = 'on_time') as on_time
                from ev group by 1, 2
            ) h
            where n >= 10 order by route_id, on_time::float / n asc
        )
        select e.route_id, e.route_short_name, count(*) as n,
               count(*) filter (where e.status = 'on_time') as on_time,
               count(*) filter (where e.status = 'early') as early,
               count(*) filter (where e.status = 'late') as late,
               percentile_cont(0.5) within group (order by e.delay_s) as median_delay,
               max(w.worst_hour) as worst_hour
        from ev e left join worst w using (route_id)
        group by 1, 2
        order by count(*) filter (where e.status = 'on_time')::float / count(*) desc
        """,
        (start, *params),
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
    st.caption(line + " On time = no more than 1 min early or 5 min late, at timepoints.")


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


def download_button(df: pd.DataFrame, filename: str, label: str = "Download CSV") -> None:
    st.download_button(
        label, df.to_csv(index=False).encode("utf-8"), file_name=filename, mime="text/csv"
    )


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


def stop_buses(
    stop_id: str, per_route: int = STOP_ROWS_PER_ROUTE
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """The next and the last `per_route` buses of each route and direction at a stop, however
    far away in time. Returns (coming, left).

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
            from classed where departed
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
