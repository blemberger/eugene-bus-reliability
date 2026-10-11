"""Stops: how late buses run at all stops together, how reliable is my stop, and how early
should I get there?"""

from __future__ import annotations

from datetime import time as dtime

import pandas as pd
import pydeck as pdk
import streamlit as st
from common import (
    ALL_ROUTES,
    JUST_LEFT_MINUTES,
    LATENESS_CHART_NOTE,
    LINES_HELP,
    LIVE_CHECK_SECONDS,
    NETWORK,
    NOT_ENOUGH_HOURLY,
    PREDICTION_OFF_NOTE,
    RANGE_HELP,
    REPORT_TABLE_HEIGHT,
    STOP_ROWS_PER_ROUTE,
    TREND_HELP,
    TYPICAL_HELP,
    back_button,
    busiest_route,
    busy_stops,
    card,
    clean_headsigns,
    col_hour,
    col_minutes,
    countdown_off_at_stop,
    countdown_off_chart,
    current_fv,
    data_note,
    day_label,
    day_sql,
    fmt_date,
    fmt_delay,
    fmt_range,
    hour_time,
    hourly_by,
    hourly_lines_chart,
    lateness,
    lateness_chart,
    line_choice,
    link_table,
    live_status_line,
    marts_ready,
    page_filters,
    q,
    range_columns,
    range_fields,
    range_scale,
    recomputing_note,
    require_db,
    require_marts,
    route_colors,
    route_link,
    route_toggles,
    search_stops,
    show_chart,
    show_coming,
    show_left,
    stop_buses,
    stop_link,
    stop_tiles,
    table,
    today_lateness,
    trend_chart,
)

require_db()
title_slot = st.container()  # the way back to every stop (once one is open) and the title
require_marts()

FV = str(current_fv())  # schedule version in force today


# map colours for a stop's typical lateness (minutes behind the timetable)
LATENESS_BANDS = [
    (-1e9, -1, "#e6a100", "typically more than 1 min early"),
    (-1, 2, "#2e8b57", "typically within 1 min early to 2 min late"),
    (2, 5, "#e57373", "typically 2 to 5 min late"),
    (5, 1e9, "#b71c1c", "typically more than 5 min late"),
]


def lateness_color(minutes: float) -> list[int]:
    for lo, hi, colour, _ in LATENESS_BANDS:
        if lo <= minutes < hi:
            return [int(colour[i : i + 2], 16) for i in (1, 3, 5)] + [220]
    return [150, 150, 150, 200]


def all_stops_chart(start, wt) -> None:
    """Before a stop is chosen: how close to the timetable buses run, hour by hour, at every stop
    together (the same chart as on the Routes page)."""
    with card():
        st.subheader("How close to the timetable, hour by hour?", help=LATENESS_CHART_NOTE)
        fig = lateness_chart(lateness(start, wt, "hour"), "all stops")
        if fig is None:
            st.caption(NOT_ENOUGH_HOURLY.format(n=10))
        else:
            show_chart(fig)


def every_stop(start, wt, table_slot, map_slot) -> None:
    """Before a stop is chosen: every stop in one sortable table (click a row to open its report
    card), and on a map coloured by its typical lateness (click a dot to open it)."""
    per = lateness(start, wt, "stop")
    if per.empty:
        with table_slot:
            if marts_ready() and lateness(start, None, "overall").empty:
                recomputing_note()
            else:
                st.info("No scored arrivals in the selected period yet.")
        return
    period = f"{day_label(wt).capitalize()} since {fmt_date(start)}"
    names = q(f"""
        select stop_id, stop_name, stop_code, stop_lat as lat, stop_lon as lon
        from gtfs.stops where feed_version_id = {FV}
    """)
    per = per.merge(names, on="stop_id", how="inner").rename(columns={"routes_here": "routes"})
    per["typical"] = per["median_delay_s"] / 60

    enough = per[per["n"] >= 20].sort_values("stop_name")
    lo, hi = range_scale(enough)
    with table_slot, card():
        st.subheader(
            "Every stop",
            help=f"{period}, stops with at least 20 measured arrivals (the filters below). Click "
            "a column heading to "
            "sort, again to reverse: Typical bus sorts latest first, then earliest first.",
        )
        link_table(
            [
                {
                    "href": stop_link(r.stop_id, "").split("#")[0],
                    "hover": f"{r.stop_name}: {int(r.n):,} arrivals measured",
                    "stop": f"{r.stop_name}" + (f" · #{r.stop_code}" if r.stop_code else ""),
                    "routes": r.routes,
                    "typical": fmt_delay(r.median_delay_s),
                    "typical_s": float(r.median_delay_s),
                    **range_fields(r.p10_delay_s, r.median_delay_s, r.p90_delay_s, lo, hi),
                }
                for r in enough.itertuples()
            ],
            [
                {"key": "stop", "label": "Stop", "width": "minmax(10em, 2fr)", "bold": True},
                {
                    "key": "routes",
                    "label": "Routes",
                    "width": "minmax(4em, 0.6fr)",
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
            ],
            max_height=520,
            key="every_stop",
            default_sort="stop:asc",
        )

    with map_slot, card():
        st.subheader(
            "Every stop on a map",
            help="Each dot is a stop, coloured by how late its typical bus is against the "
            "timetable. " + f"{period}; stops with at least 20 arrivals.",
        )
        shown = enough.copy()
        shown["color"] = shown["typical"].map(lateness_color)
        shown["typical_txt"] = shown["typical"].map(lambda m: fmt_delay(m * 60))
        shown["code"] = shown["stop_code"].map(lambda c: f"#{c}" if c else "")
        picked_map = st.pydeck_chart(
            pdk.Deck(
                layers=[
                    pdk.Layer(
                        "ScatterplotLayer",
                        id="stops",
                        data=shown,
                        get_position="[lon, lat]",
                        get_fill_color="color",
                        get_line_color=[255, 255, 255],
                        stroked=True,
                        line_width_min_pixels=1,
                        get_radius=14,
                        radius_min_pixels=4,
                        radius_max_pixels=9,
                        pickable=True,
                    )
                ],
                initial_view_state=pdk.ViewState(latitude=44.05, longitude=-123.07, zoom=11.5),
                map_style=None,
                tooltip={
                    "html": "<b>{stop_name}</b> {code}<br/>routes {routes}<br/>typical bus {typical_txt}"
                    "<br/>{n} arrivals<br/>click to open this stop"
                },
            ),
            height=460,
            on_select="rerun",
            selection_mode="single-object",
            key="stop_overview_map",
        )
        st.caption(
            "Typical bus: "
            + " · ".join(
                f"<span style='color:{c}'>●</span> {label.replace('typically ', '')}"
                for _, _, c, label in LATENESS_BANDS
            )
            + ". Click a stop to open it.",
            unsafe_allow_html=True,
        )
        objs = (picked_map.selection.objects or {}).get("stops") if picked_map is not None else None
        clicked = objs[0]["stop_id"] if objs else None
        # only a new click counts (the map keeps its last selection when it is drawn again)
        if clicked and clicked != st.session_state.get("stop_overview_last"):
            st.session_state["stop_overview_last"] = clicked
            st.query_params["stop"] = clicked
            st.rerun()


def stop_finder() -> None:
    """Search by name or sign number (matches appear as cards that open the stop), and the
    busiest stops as cards until something is typed."""
    example = q(f"""
        select stop_code, stop_name from gtfs.stops
        where feed_version_id = {FV} and location_type = 0 and stop_code is not null
          and stop_code <> ''
        order by stop_name limit 1
    """)
    hint = (
        f"Search: stop name or the number on the sign, e.g. {example['stop_name'][0]} or "
        f"{example['stop_code'][0]}"
        if not example.empty
        else "Search: stop name or the number on the sign"
    )
    query = st.text_input(
        "Stop name or the number on the sign",
        placeholder=hint,
        key="stop_search",
        label_visibility="collapsed",
    )
    if query:
        matches = search_stops(query)
        if matches.empty:
            st.warning("No stop matches that. Try part of a street name.")
        else:
            stop_tiles(matches)
    else:
        st.caption("Busy stops")
        stop_tiles(busy_stops(12))


# The stop comes from the page address only (?stop=...), so the Stops tab, the "All stops"
# button and the browser's back button always lead to the list of every stop.
stop_id = st.query_params.get("stop")
if not stop_id:
    # no stop yet: find one, every stop in a table, the filters, every stop on a map, all stops
    # together
    title_slot.title("How reliable is my stop?")
    with card():
        st.subheader("Find your stop")
        stop_finder()
    # every stop in a table and on a map, every stop together hour by hour, then the period and
    # days filters they all follow (drawn in that order; the filters are read first), as on the
    # Routes page
    every_slot, map_slot, chart_slot = st.container(), st.container(), st.container()
    start, wt = page_filters()
    every_stop(start, wt, every_slot, map_slot)
    with chart_slot:
        all_stops_chart(start, wt)
    st.divider()
    data_note(start)
    st.stop()
found = q(
    f"select stop_id, stop_code, stop_name, stop_lat, stop_lon from gtfs.stops where stop_id = %s and feed_version_id = {FV}",
    (stop_id,),
)


def _all_stops() -> None:
    """Back to the list of every stop."""
    st.query_params.pop("stop", None)


with title_slot:
    back_button("← All stops", on_click=_all_stops)
    if found.empty:
        st.title("How reliable is my stop?")
        st.warning("That stop isn't in the current timetable.")
        st.stop()
stop = found.iloc[0]
code = f" (#{stop['stop_code']})" if stop["stop_code"] else ""
st.set_page_config(page_title=f"{stop['stop_name']}{code}: how reliable? · Eugene Bus Watch")
with title_slot:
    st.title(f"How reliable is the stop at {stop['stop_name']}{code}?")
    st.markdown(
        "[Where is it? Open in Google Maps ↗](https://www.google.com/maps/search/?api=1&query="
        f"{float(stop['stop_lat']):.6f},{float(stop['stop_lon']):.6f})"
    )


# ---- right now at this stop ------------------------------------------------------------
@st.fragment(run_every=f"{LIVE_CHECK_SECONDS}s")
def stop_right_now() -> None:
    live_status_line()
    coming, left = stop_buses(stop_id)
    n = STOP_ROWS_PER_ROUTE
    st.markdown(f"**Coming up (next {n} of each route)**")
    show_coming(coming, "No buses scheduled at this stop in the next 36 hours.")
    st.markdown(f"**Just left (last {JUST_LEFT_MINUTES} minutes)**")
    show_left(left, f"No bus has left this stop in the last {JUST_LEFT_MINUTES} minutes.")


# ---- reliability: the routes table and the hour-by-hour chart, then the period and days
# filters that they and everything below follow (drawn in that order; the filters are read first)
routes_slot, hour_slot = st.container(), st.container()
start, wt = page_filters()
wt_clause, wt_params = day_sql(wt)
period = f"{day_label(wt)} since {fmt_date(start)}"


def route_key(v) -> tuple:
    v = str(v)
    return (len(v), v)


by_route = q(
    f"""
    select route_id, route_short_name as route, direction_id, count(*) as n,
           count(*) filter (where status = 'on_time') as on_time,
           count(*) filter (where status = 'early') as early,
           count(*) filter (where status = 'late') as late,
           percentile_cont(0.5) within group (order by delay_s) as median_delay,
           percentile_cont(0.1) within group (order by delay_s) as p10,
           percentile_cont(0.9) within group (order by delay_s) as p90,
           min(service_date) as first_day, max(service_date) as last_day,
           count(distinct service_date) as n_days
    from marts.fct_stop_events
    where stop_id = %s and status is not null and service_date >= %s {wt_clause}
    group by 1, 2, 3 order by 2, 3
    """,
    (stop_id, start, *wt_params),
)
if by_route.empty:
    with routes_slot:
        st.info("No scored arrivals at this stop in the selected period yet.")
        st.markdown("#### Right now at this stop")
        stop_right_now()
    st.stop()

headsigns = q(
    f"""
    select distinct t.route_id, r.route_short_name as route, t.direction_id, t.trip_headsign as headsign
    from gtfs.stop_times st
    join gtfs.trips t on t.trip_id = st.trip_id and t.feed_version_id = st.feed_version_id
    join gtfs.routes r on r.route_id = t.route_id and r.feed_version_id = t.feed_version_id
    where st.stop_id = %s and st.feed_version_id = {FV}
    """,
    (stop_id,),
)
headsigns["headsign"] = clean_headsigns(headsigns)
hs = (
    headsigns[headsigns["headsign"] != ""]
    .groupby(["route", "direction_id"])["headsign"]
    .agg(lambda x: " / ".join(sorted(set(x))[:2]))
    .reset_index()
)
tbl = by_route.merge(hs, on=["route", "direction_id"], how="left")
# One name per route and direction: just the route where it passes here in one direction,
# "route → destination" where it passes in both.
both_ways = set(tbl.groupby("route")["direction_id"].nunique().loc[lambda s: s > 1].index)
both_way_ids = {
    str(r) for r, rt in zip(tbl["route_id"], tbl["route"], strict=False) if rt in both_ways
}
tbl["label"] = [
    (f"{r} → {h}" if isinstance(h, str) and h else f"{r} (direction {d})") if r in both_ways else r
    for r, h, d in zip(tbl["route"], tbl["headsign"], tbl["direction_id"], strict=False)
]
tbl = tbl.assign(rkey=tbl["route"].map(route_key)).sort_values(["rkey", "direction_id"])
labels = dict(zip(zip(tbl["route"], tbl["direction_id"], strict=False), tbl["label"], strict=False))

here = q(
    f"""
    select percentile_cont(0.5) within group (order by delay_s) as median_delay,
           percentile_cont(0.1) within group (order by delay_s) as p10,
           percentile_cont(0.9) within group (order by delay_s) as p90, count(*) as n
    from marts.fct_stop_events
    where stop_id = %s and status is not null and service_date >= %s {wt_clause}
    """,
    (stop_id, start, *wt_params),
).iloc[0]
today = today_lateness(stop_id=stop_id)
n_today = int(today["n"].iloc[0]) if len(today) else 0
with routes_slot, card():
    st.subheader(
        "Every route at this stop",
        help=f"{period[0].upper() + period[1:]}. Click a column heading to sort.",
    )
    c1, c2, c3 = st.columns(3)
    c1.metric(
        "Typical bus here vs timetable",
        fmt_delay(here["median_delay"]),
        help=TYPICAL_HELP
        + f" {int(here['n']):,} arrivals measured, {fmt_date(by_route['first_day'].min())} to "
        f"{fmt_date(by_route['last_day'].max())}.",
    )
    c2.metric("8 in 10 buses", fmt_range(here["p10"], here["p90"]), help=RANGE_HELP)
    c3.metric(
        "Today so far",
        fmt_delay(today["median_delay_s"].iloc[0]) if n_today >= 10 else "—",
        help="The typical bus here today, as of the latest update (every 15 minutes)"
        + (f", from {n_today:,} arrivals." if n_today else "; none measured yet."),
    )
    for c in ("median_delay", "p10", "p90"):
        tbl[c] = tbl[c].astype(float)
    lo, hi = range_scale(tbl, "p10", "p90")
    link_table(
        [
            {
                "href": route_link(r.route_id, r.route).split("#")[0],
                "hover": f"Route {r.route} here: {int(r.n):,} arrivals measured",
                "route": r.route,
                "toward": r.headsign if isinstance(r.headsign, str) else "",
                "typical": fmt_delay(r.median_delay),
                "typical_s": float(r.median_delay),
                **range_fields(r.p10, r.median_delay, r.p90, lo, hi),
            }
            for r in tbl.itertuples()
        ],
        [
            {"key": "route", "label": "Route", "width": "3.4em", "bold": True},
            {"key": "toward", "label": "Toward", "width": "minmax(6em, 1.2fr)"},
            {
                "key": "typical",
                "label": "Typical bus",
                "width": "7.6em",
                "help": TYPICAL_HELP,
                "sort": "typical_s",
            },
            *range_columns(lo, hi),
        ],
        key="stop_routes",
        default_sort="route:asc",
        max_height=REPORT_TABLE_HEIGHT,
    )

# ---- by hour: every route here together, and any route on its own ---------------------------
with hour_slot, card():
    st.subheader(
        "How close to the timetable, hour by hour?",
        help=LINES_HELP.format(all="every route here together", one="route")
        + f" {period[0].upper() + period[1:]}.",
    )
    # one line per route and direction (a route passing here both ways gets one per direction)
    hourly = hourly_by(
        "route_id || ':' || coalesce(direction_id, -1)", "and stop_id = %s", (stop_id,), start, wt
    )
    keys = [
        f"{r}:{-1 if pd.isna(d) else int(d)}"
        for r, d in zip(tbl["route_id"], tbl["direction_id"], strict=False)
    ]
    # buttons say just the route (as on the Predictions page); the chart's key says "route ..."
    key_label = {
        f"{r}:{-1 if pd.isna(d) else int(d)}": str(lab)
        for r, d, lab in zip(tbl["route_id"], tbl["direction_id"], tbl["label"], strict=False)
    }
    shown = line_choice(
        keys, key_label, [ALL_ROUTES], key=f"stop_hour_lines_{stop_id}", all_label="All routes here"
    )
    colors = route_colors()
    lines = [
        {
            "label": "all routes here" if k == ALL_ROUTES else f"route {key_label[k]}",
            "df": hourly[hourly["key"] == k],
            "color": "#1f5f9e" if k == ALL_ROUTES else colors.get(k.split(":")[0], "#555555"),
            "dash": "dot" if k.endswith(":1") and k.split(":")[0] in both_way_ids else "solid",
        }
        for k in shown
    ]
    fig = hourly_lines_chart(
        lines, reference=lateness(start, wt, "hour"), reference_label=NETWORK, min_n=5
    )
    if not shown:
        st.caption("Pick a line above.")
    elif fig is None:
        st.caption(NOT_ENOUGH_HOURLY.format(n=5))
    else:
        show_chart(fig)


# ---- arrive-by guidance ---------------------------------------------------
with card():
    st.subheader("How early should I be at the stop?")
    st.caption(
        "How many minutes before the scheduled time to be at the stop to catch 19 buses in 20. "
        f"{period[0].upper() + period[1:]}; a route needs 20 arrivals here."
    )
    early_all = q(
        f"""
        select route_id, route_short_name as route, direction_id, count(*) as n,
               percentile_cont(0.05) within group (order by delay_s) as p05
        from marts.fct_stop_events
        where stop_id = %s and status is not null and service_date >= %s {wt_clause}
        group by 1, 2, 3
        """,
        (stop_id, start, *wt_params),
    )
    early_all = early_all.merge(hs, on=["route", "direction_id"], how="left")
    early_all["rkey"] = early_all["route"].map(route_key)
    early_all = early_all.sort_values(["rkey", "direction_id"])

    def early_by(p05, n) -> float | None:
        if n < 20 or p05 is None or pd.isna(p05):
            return None
        return 0.0 if p05 >= -30 else float(round(-float(p05) / 60))

    rows = []
    for r in early_all.itertuples():
        e = early_by(r.p05, r.n)
        rows.append(
            {
                "href": route_link(r.route_id, r.route).split("#")[0],
                "hover": f"Route {r.route} here: {int(r.n):,} arrivals measured",
                "route": r.route,
                "toward": r.headsign if isinstance(r.headsign, str) else "",
                "early": "not enough data yet"
                if e is None
                else ("on time is enough" if e == 0 else f"{e:.0f} min"),
                "early_s": e,
            }
        )
    link_table(
        rows,
        [
            {"key": "route", "label": "Route", "width": "3.4em", "bold": True},
            {"key": "toward", "label": "Toward", "width": "minmax(6em, 1.5fr)"},
            {
                "key": "early",
                "label": "Be there early by",
                "width": "minmax(9em, 1fr)",
                "sort": "early_s",
                "first": "desc",
            },
        ],
        key="stop_early",
        default_sort="route:asc",
    )
    with st.expander("Hour by hour"):
        guide = q(
            f"""
            select route_short_name as route, direction_id, hour_local, count(*) as n,
                   percentile_cont(0.05) within group (order by delay_s) as p05
            from marts.fct_stop_events
            where stop_id = %s and status is not null and service_date >= %s {wt_clause}
            group by 1, 2, 3 having count(*) >= 10
            """,
            (stop_id, start, *wt_params),
        )
        guide["label"] = [
            labels.get((r, d), r)
            for r, d in zip(guide["route"], guide["direction_id"], strict=False)
        ]
        # minutes before the scheduled time; 0 when the earliest buses are no more than 30 s early
        guide["early_by"] = guide["p05"].map(
            lambda s: 0.0 if s is None or pd.isna(s) or s >= -30 else round(-float(s) / 60)
        )
        if guide.empty:
            st.caption("Not enough arrivals per hour yet (needs 10+ in an hour).")
        else:
            guide["col"] = "Route " + guide["label"].astype(str)
            pivot = guide.pivot_table(
                index="hour_local", columns="col", values="early_by", aggfunc="first"
            ).sort_index()
            order = ["Route " + o for o in tbl["label"]]
            pivot = pivot[[c for c in order if c in pivot.columns]]
            pivot.insert(0, "Hour", [hour_time(h) for h in pivot.index])
            table(
                pivot.reset_index(drop=True),
                hide_index=True,
                width="stretch",
                column_config={"Hour": col_hour("Hour")}
                | {
                    c: col_minutes(
                        c,
                        help="Be at the stop this many minutes before the scheduled time (0 = no need).",
                    )
                    for c in pivot.columns
                    if c != "Hour"
                },
            )
            st.caption("Blank where an hour has fewer than 10 arrivals.")


# ---- each scheduled bus ---------------------------------------------------------------------
with card():
    st.subheader(
        "Each scheduled departure",
        help="How each scheduled bus of this route usually runs here against the timetable "
        "(negative = early), and the earliest and latest it came. A departure shows once it has "
        f"been measured on 3 days; hover a row for how many. {period[0].upper() + period[1:]}.",
    )
    pairs = list(zip(tbl["route"], tbl["direction_id"], strict=False))
    usual_key = st.selectbox(
        "Route",
        pairs,
        format_func=lambda k: labels.get(k, str(k[0])),
        key=f"usual_route_{stop_id}",
    )
    usual = q(
        f"""
        select to_char(scheduled_arrival at time zone 'America/Los_Angeles', 'HH24:MI') as sched,
               weekday_type, count(*) as n,
               percentile_cont(0.5) within group (order by delay_s) as median_delay,
               min(delay_s) as earliest, max(delay_s) as latest
        from marts.fct_stop_events
        where stop_id = %s and route_short_name = %s and direction_id is not distinct from %s
          and status is not null and service_date >= %s {wt_clause}
        group by 1, 2 having count(*) >= 3
        """,
        (
            stop_id,
            usual_key[0],
            None if pd.isna(usual_key[1]) else int(usual_key[1]),
            start,
            *wt_params,
        ),
    )
    if usual.empty:
        st.caption(
            "Not enough days yet: a scheduled bus shows once it has been measured on 3 days "
            f"({period})."
        )
    else:
        hhmm = usual["sched"].str.split(":", expand=True).astype(int)
        usual["minute"] = hhmm[0] * 60 + hhmm[1]
        # service-day order: 4 am first, after-midnight trips last
        usual["service_min"] = (usual["minute"] - 240) % 1440
        usual = usual.sort_values("service_min")
        days_word = {"weekday": "Weekdays", "saturday": "Saturdays", "sunday": "Sundays"}
        cols = [
            {
                "key": "sched",
                "label": "Scheduled here",
                "width": "minmax(6em, 1fr)",
                "bold": True,
                "sort": "sched_s",
                "first": "asc",
            },
            {
                "key": "typical",
                "label": "Typical bus",
                "width": "minmax(7em, 1fr)",
                "help": TYPICAL_HELP,
                "sort": "typical_s",
            },
            {
                "key": "earliest",
                "label": "Earliest",
                "width": "minmax(6em, 1fr)",
                "help": "The earliest this bus came on any day measured.",
                "sort": "earliest_s",
                "first": "asc",
            },
            {
                "key": "latest",
                "label": "Latest",
                "width": "minmax(6em, 1fr)",
                "help": "The latest this bus came on any day measured.",
                "sort": "latest_s",
            },
        ]
        if wt is None:  # all days: weekday and weekend timetables differ, so say which
            cols.insert(1, {"key": "runs", "label": "Runs", "width": "minmax(5.5em, 0.8fr)"})
        link_table(
            [
                {
                    "hover": f"Measured on {int(r.n)} days",
                    "sched": dtime(int(r.minute) // 60 % 24, int(r.minute) % 60)
                    .strftime("%I:%M %p")
                    .lstrip("0")
                    .lower(),
                    "sched_s": int(r.service_min),
                    "runs": days_word.get(r.weekday_type, ""),
                    "typical": fmt_delay(r.median_delay),
                    "typical_s": float(r.median_delay),
                    "earliest": fmt_delay(r.earliest),
                    "earliest_s": float(r.earliest),
                    "latest": fmt_delay(r.latest),
                    "latest_s": float(r.latest),
                }
                for r in usual.itertuples()
            ],
            cols,
            max_height=520,
            key="stop_usual",
            default_sort="sched:asc",
        )


# ---- right now ------------------------------------------------------------------------------
with card():
    st.subheader(
        "Right now at this stop",
        help="The next buses of each route and direction, however far away, and any that left "
        f"in the last {JUST_LEFT_MINUTES} minutes. Arrives and In are LTD's predictions where it "
        "has one; LTD only predicts trips that are about to run, so later buses show the "
        "timetable. Likely in corrects LTD's prediction by how far off it has usually been "
        "(same route, time of day, minutes ahead). Min late compares with the timetable; "
        "negative = early.",
    )
    stop_right_now()


# ---- trend --------------------------------------------------------------------
with card():
    st.subheader("Is it getting better?", help=TREND_HELP + " All routes at this stop.")
    trend_chart("and stop_id = %s", (stop_id,), "here")


# ---- how far off LTD's predictions are here ------------------------------------------------
with card():
    st.subheader(
        "How far off are the predictions at this stop?",
        help=PREDICTION_OFF_NOTE
        + f" {period[0].upper() + period[1:]}. More on the Predictions page.",
    )
    here_routes = tbl[["route_id", "route"]].drop_duplicates("route_id")
    here_names = dict(
        zip(here_routes["route_id"].astype(str), here_routes["route"].astype(str), strict=False)
    )
    at_stop = countdown_off_at_stop(stop_id, start, wt)
    here_ids = sorted({str(r) for r in at_stop["route_id"].dropna()})
    # every route here together, and the route with the most bus arrivals here to compare
    busiest = busiest_route(at_stop) if len(here_ids) > 1 else None
    lines = route_toggles(
        here_ids,
        here_names,
        [ALL_ROUTES, *([busiest] if busiest else [])],
        key=f"stop_pred_lines_{stop_id}",
        all_label="All routes here",
    )
    fig = countdown_off_chart(
        at_stop,
        {k: f"Route {v}" for k, v in here_names.items()},
        lines,
        all_label="All routes here",
    )
    if fig is None:
        st.caption("Not enough measured arrivals with a prediction here yet for this period.")
    else:
        show_chart(fig)

# ---- nearby stops: across the street, the next one along ------------------------------------
near = q(
    f"""
    select s.stop_id, s.stop_name, s.stop_code
    from gtfs.stops s, gtfs.stops here
    where here.stop_id = %s and here.feed_version_id = {FV} and s.feed_version_id = {FV}
      and s.location_type = 0 and s.stop_id <> here.stop_id
      and s.stop_id in (select distinct stop_id from marts.fct_stop_events
                        where service_date >= current_date - 30)
    order by st_distance(s.geom::geography, here.geom::geography)
    limit 8
    """,
    (stop_id,),
)
if not near.empty:
    with card():
        st.subheader("Nearby stops")
        stop_tiles(near)

st.divider()
data_note(start)
