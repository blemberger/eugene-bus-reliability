"""Stops: how late buses run at all stops together, how reliable is my stop, and how early
should I get there?"""

from __future__ import annotations

from datetime import time as dtime

import pandas as pd
import pydeck as pdk
import streamlit as st
from common import (
    JUST_LEFT_MINUTES,
    LIVE_CHECK_SECONDS,
    REPORT_TABLE_HEIGHT,
    STOP_ROWS_PER_ROUTE,
    back_button,
    busy_stops,
    card,
    clean_headsigns,
    current_fv,
    data_note,
    day_label,
    day_sql,
    days_suffix,
    every_stop_card,
    filters_again,
    fmt_date,
    fmt_delay,
    headline_metrics,
    lateness,
    link_table,
    live_status_line,
    marts_ready,
    page_filters,
    predictions_card,
    q,
    range_columns,
    range_fields,
    range_scale,
    recomputing_note,
    require_db,
    require_marts,
    route_link,
    search_stops,
    show_coming,
    show_left,
    stop_buses,
    stop_tiles,
    stop_timetable,
    stops_with_numbers,
    times_of_day_card,
    timetable_card,
    today_lateness,
    trend_card,
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
    enough = stops_with_numbers(per)
    with table_slot:
        every_stop_card(enough, period)

    with map_slot, card():
        st.subheader(
            f"Every stop on the map{days_suffix()}",
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
    # every stop in a table, then right under it the period and days buttons everything below
    # follows (drawn in that order, though the buttons are read first); every stop on a map,
    # all stops together by time of day and the predictions; the buttons again; the trend. The
    # same order as the Routes page.
    every_slot, filters_slot = st.container(), st.container()
    map_slot, chart_slot = st.container(), st.container()
    with filters_slot:
        start, wt = page_filters()
    every_stop(start, wt, every_slot, map_slot)
    period = f"{day_label(wt)} since {fmt_date(start)}"
    with chart_slot:
        times_of_day_card("stops_tod", start, wt, period)
    predictions_card("stops_pred", start, wt, period)
    filters_again()  # the period and days buttons again, between the two lower charts
    trend_card("stops_trend")
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
# the sign number stays in the browser tab's title (what a search shows), and goes on the line
# under the heading rather than in it
st.set_page_config(page_title=f"{stop['stop_name']}{code}: how reliable? · Eugene Bus Watch")
with title_slot:
    st.title(f"How reliable is the stop at {stop['stop_name']}?")
    st.markdown(
        (f"Stop #{stop['stop_code']} · " if stop["stop_code"] else "")
        + "[Where is it? Open in Google Maps ↗](https://www.google.com/maps/search/?api=1&query="
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


# ---- reliability: the chart by time of day, then right under it the period and days buttons
# everything on the page follows (drawn in that order, though the buttons are read first), then
# the routes table; the buttons come again after the predictions
hour_slot, filters_slot, routes_slot = st.container(), st.container(), st.container()
with filters_slot:
    start, wt = page_filters()
wt_clause, wt_params = day_sql(wt)
period = f"{day_label(wt)} since {fmt_date(start)}"


def route_key(v) -> tuple:
    v = str(v)
    return (len(v), v)


by_route = q(
    f"""
    select route_id, route_short_name as route, direction_id, count(*) as n,
           avg(abs(delay_s)) as mean_abs,
           count(*) filter (where status = 'early') as early,
           count(*) filter (where status = 'late') as late,
           percentile_cont(0.5) within group (order by delay_s) as median_delay,
           percentile_cont(0.1) within group (order by delay_s) as p10,
           percentile_cont(0.9) within group (order by delay_s) as p90,
           percentile_cont(0.05) within group (order by delay_s) as p05,
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
        st.markdown(f"#### Right now at {stop['stop_name']}")
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

SNAME = str(stop["stop_name"])  # the stop as titles name it
here = q(
    f"""
    select percentile_cont(0.5) within group (order by delay_s) as median_delay_s,
           percentile_cont(0.1) within group (order by delay_s) as p10_delay_s,
           percentile_cont(0.9) within group (order by delay_s) as p90_delay_s, count(*) as n,
           avg(abs(delay_s)) as mean_abs_delay_s
    from marts.fct_stop_events
    where stop_id = %s and status is not null and service_date >= %s {wt_clause}
    """,
    (stop_id, start, *wt_params),
).iloc[0]


def early_fields(p05, n) -> dict:
    """'Be early by': minutes before the scheduled time to be at the stop to catch 19 buses in
    20 (the 5th percentile of lateness); on time when those are no more than 30 s early."""
    if n < 20 or p05 is None or pd.isna(p05):
        return {"early": "—", "early_s": None}
    m = 0.0 if p05 >= -30 else float(round(-float(p05) / 60))
    return {"early": "on time" if m == 0 else f"{m:.0f} min", "early_s": m}


with routes_slot, card():
    st.subheader(
        f"Each route at {SNAME}{days_suffix()}",
        help=f"{period[0].upper() + period[1:]}. Click a column heading to sort.",
    )
    headline_metrics(here, today_lateness(stop_id=stop_id), start, " here vs timetable")
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
                **range_fields(r.p10, r.median_delay, r.p90, lo, hi, r.mean_abs),
                **early_fields(r.p05, r.n),
            }
            for r in tbl.itertuples()
        ],
        [
            {"key": "route", "label": "Route", "width": "3.8em", "bold": True},
            {"key": "toward", "label": "Toward", "width": "minmax(6em, 1.2fr)"},
            *range_columns(lo, hi),
            {
                "key": "early",
                "label": "Be early by",
                "width": "6.6em",
                "num": True,
                "help": "How many minutes before the scheduled time to be at the stop to catch "
                "19 buses in 20 (a route needs 20 arrivals here). On time: the earliest buses "
                "are no more than 30 seconds early. Sorts the most first.",
                "sort": "early_s",
                "first": "desc",
            },
        ],
        key="stop_routes",
        default_sort="route:asc",
        max_height=REPORT_TABLE_HEIGHT,
    )

# ---- times of day: every route here, or one ------------------------------------------------
with hour_slot:
    times_of_day_card("stop_tod", start, wt, period, fixed_stop=(stop_id, SNAME))


# ---- the predictions, then the trend: every route here, or one ------------------------------
predictions_card("stop_pred", start, wt, period, fixed_stop=(stop_id, SNAME))
filters_again()  # the period and days buttons again, for everything below
trend_card("stop_trend", fixed_stop=(stop_id, SNAME))


# ---- each scheduled bus ---------------------------------------------------------------------
MIN_DEPARTURE_DAYS = 5  # fewer days than this make a poor "8 in 10"
with card():
    st.subheader(
        f"Each scheduled departure from {SNAME}{days_suffix()}",
        help="How each scheduled bus of this route runs here against the timetable, day after "
        "day: minutes off on average, the typical day, and the range 8 days in 10 fell in. A "
        f"departure shows once it has been measured on {MIN_DEPARTURE_DAYS} days; hover a row "
        f"for how many. {period[0].upper() + period[1:]}.",
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
               weekday_type, count(*) as n, avg(abs(delay_s)) as mean_abs,
               percentile_cont(0.5) within group (order by delay_s) as median_delay,
               percentile_cont(0.1) within group (order by delay_s) as p10,
               percentile_cont(0.9) within group (order by delay_s) as p90
        from marts.fct_stop_events
        where stop_id = %s and route_short_name = %s and direction_id is not distinct from %s
          and status is not null and service_date >= %s {wt_clause}
        group by 1, 2 having count(*) >= {MIN_DEPARTURE_DAYS}
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
            "Not enough days yet: a scheduled bus shows once it has been measured on "
            f"{MIN_DEPARTURE_DAYS} days ({period})."
        )
    else:
        hhmm = usual["sched"].str.split(":", expand=True).astype(int)
        usual["minute"] = hhmm[0] * 60 + hhmm[1]
        # service-day order: 4 am first, after-midnight trips last
        usual["service_min"] = (usual["minute"] - 240) % 1440
        usual = usual.sort_values("service_min")
        for c in ("median_delay", "p10", "p90", "mean_abs"):
            usual[c] = usual[c].astype(float)
        u_lo, u_hi = range_scale(usual, "p10", "p90")
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
            *range_columns(u_lo, u_hi),
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
                    **range_fields(r.p10, r.median_delay, r.p90, u_lo, u_hi, r.mean_abs),
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
        f"Right now at {SNAME}",
        help="The next buses of each route and direction, however far away, and any that left "
        f"in the last {JUST_LEFT_MINUTES} minutes. Arrives and In are LTD's predictions where it "
        "has one; LTD only predicts trips that are about to run, so later buses show the "
        "timetable. Likely in corrects LTD's prediction by how far off it has usually been "
        "(same route, time of day, minutes ahead). Min late compares with the timetable; "
        "negative = early.",
    )
    stop_right_now()
    st.page_link(
        "views/accuracy.py",
        label="How LTD's predictions for these buses have changed →",
        query_params={"stop": stop_id},
    )

# ---- the plain timetable, folded away: for reference --------------------------------------
timetable_card(
    f"Timetable at {SNAME}, every route",
    f"tt_stop_{stop_id}",
    lambda day: stop_timetable(stop_id, day),
)


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
        st.subheader(f"Stops near {SNAME}")
        stop_tiles(near)

st.divider()
data_note(start)
