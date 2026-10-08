"""Stops: how late buses run at all stops together, how reliable is my stop, and how early
should I get there?"""

from __future__ import annotations

from datetime import time as dtime

import pandas as pd
import plotly.graph_objects as go
import pydeck as pdk
import streamlit as st
from common import (
    COUNTDOWN_OFF_NOTE,
    JUST_LEFT_MINUTES,
    LATENESS_CHART_NOTE,
    LIVE_CHECK_SECONDS,
    RANGE_HELP,
    STOP_ROWS_PER_ROUTE,
    TYPICAL_HELP,
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
    fit_phone,
    fmt_date,
    fmt_delay,
    fmt_range,
    hour_label,
    hour_time,
    lateness,
    lateness_chart,
    link_table,
    live_status_line,
    marts_ready,
    minutes_axis,
    on_time_line,
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
    search_stops,
    service_hour_key,
    show_coming,
    show_left,
    stop_buses,
    stop_buttons,
    stop_link,
    table,
    today_lateness,
)

require_db()
st.title("How reliable is my stop?")
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


def all_stops_summary(start, wt) -> None:
    """Before a stop is chosen, at the bottom: how late buses run by hour at all stops together."""
    tot = lateness(start, wt, "overall")
    if tot.empty:
        return
    with card():
        st.subheader(
            f"All stops together, {day_label(wt)}",
            help=LATENESS_CHART_NOTE + " The same as all routes together on the Overview.",
        )
        fig = lateness_chart(lateness(start, wt, "hour"), "all stops")
        if fig is not None:
            st.plotly_chart(fit_phone(fig), width="stretch")
        t = tot.iloc[0]
        c1, c2 = st.columns(2)
        c1.metric(
            "Typical bus vs timetable",
            fmt_delay(t["median_delay_s"]),
            help=TYPICAL_HELP + f" {int(t['n']):,} arrivals measured since {fmt_date(start)}.",
        )
        c2.metric("8 in 10 buses", fmt_range(t["p10_delay_s"], t["p90_delay_s"]), help=RANGE_HELP)


def every_stop(start, wt) -> None:
    """Before a stop is chosen: every stop in one sortable table (click a row to open its report
    card), and on a map coloured by its typical lateness (click a dot to open it)."""
    per = lateness(start, wt, "stop")
    if per.empty:
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
    with card():
        st.subheader(
            "Every stop",
            help=f"{period}, stops with at least 20 measured arrivals. Click a column heading to "
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
                    "width": "minmax(4em, 0.7fr)",
                    "hide_on_phone": True,
                },
                {
                    "key": "typical",
                    "label": "Typical bus",
                    "width": "minmax(7em, 0.8fr)",
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
        st.caption("Click a stop for its report card.")

    with card():
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
        if clicked and clicked != st.session_state.get("stop_overview_last"):
            st.session_state["stop_overview_last"] = clicked
            st.session_state["stop_id"] = clicked
            st.rerun()


def stop_button_grid(df: pd.DataFrame, key_prefix: str, show_code: bool = False) -> None:
    clicked = stop_buttons(df, key_prefix, show_code=show_code)
    if clicked:
        st.session_state["stop_id"] = clicked
        st.session_state["_clear_stop_search"] = True
        st.rerun()  # redraw for the chosen stop


def stop_finder(with_map: bool) -> None:
    """Search by name or sign number (matches appear as buttons), the busiest stops as buttons
    until something is typed, and (once a stop is chosen, inside "Choose a different stop") a
    map to click a stop on; before a stop is chosen the coloured map further down does that."""
    if st.session_state.pop("_clear_stop_search", False):
        st.session_state["stop_search"] = ""
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
            stop_button_grid(matches, "stop", show_code=True)
    else:
        stop_button_grid(busy_stops(9), "busy")
    if not with_map:
        return
    pts = q(f"""
        select stop_id, stop_code, stop_name, stop_lat as lat, stop_lon as lon from gtfs.stops
        where feed_version_id = {FV} and location_type = 0
    """)
    pts["code"] = pts["stop_code"].map(lambda c: f"#{c}" if c else "")
    picked_map = st.pydeck_chart(
        pdk.Deck(
            layers=[
                pdk.Layer(
                    "ScatterplotLayer",
                    id="stops",
                    data=pts,
                    get_position="[lon, lat]",
                    get_fill_color=[11, 110, 79, 200],
                    get_line_color=[255, 255, 255],
                    stroked=True,
                    line_width_min_pixels=1,
                    get_radius=12,
                    radius_min_pixels=4,
                    radius_max_pixels=9,
                    pickable=True,
                )
            ],
            initial_view_state=pdk.ViewState(latitude=44.05, longitude=-123.09, zoom=12),
            map_style=None,
            tooltip={"html": "<b>{stop_name}</b> {code}<br/>click to open this stop"},
        ),
        height=420,
        on_select="rerun",
        selection_mode="single-object",
        key="stop_pick_map",
    )
    objs = (picked_map.selection.objects or {}).get("stops") if picked_map is not None else None
    clicked = objs[0]["stop_id"] if objs else None
    # only a new click counts, so it doesn't override later picks made another way
    if clicked and clicked != st.session_state.get("stop_pick_map_last"):
        st.session_state["stop_pick_map_last"] = clicked
        st.session_state["stop_id"] = clicked
        st.rerun()


# a stop arriving in the link opens straight away
if "stop" in st.query_params and not st.session_state.get("stop_id"):
    st.session_state["stop_id"] = st.query_params["stop"]

stop_id = st.session_state.get("stop_id")
if not stop_id:
    # no stop yet: find one, every stop in a table and on a map, all stops together
    with card():
        st.subheader("Find your stop")
        stop_finder(with_map=False)
    start, wt = page_filters()
    every_stop(start, wt)
    all_stops_summary(start, wt)
    st.divider()
    data_note(start)
    st.stop()
st.query_params["stop"] = stop_id
stop = q(
    f"select stop_id, stop_code, stop_name, stop_lat, stop_lon from gtfs.stops where stop_id = %s and feed_version_id = {FV}",
    (stop_id,),
).iloc[0]
st.subheader(f"{stop['stop_name']}" + (f"  ·  #{stop['stop_code']}" if stop["stop_code"] else ""))
st.page_link("views/map.py", label="What's coming to this stop right now →")
with st.expander("Choose a different stop"):
    stop_finder(with_map=True)


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


with st.expander("Where is this stop?", expanded=False):
    st.map(
        pd.DataFrame({"lat": [float(stop["stop_lat"])], "lon": [float(stop["stop_lon"])]}),
        zoom=15,
        size=25,
        height=260,
    )

# ---- reliability: everything down to "Right now" follows these filters ---------------------
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
with card():
    st.subheader(
        "Routes at this stop",
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
                "width": "minmax(7em, 0.8fr)",
                "help": TYPICAL_HELP,
                "sort": "typical_s",
            },
            *range_columns(lo, hi),
        ],
        key="stop_routes",
        default_sort="route:asc",
    )
    st.caption("Click a route for its report card.")

# ---- when are buses late here: by hour, one line per route --------------------------------
with card():
    st.subheader(
        "When are buses late at this stop?",
        help="Against the printed timetable, by the hour the bus was scheduled. Each line is "
        "the typical bus (the median); below zero = early. With one line showing, the band is "
        "where 8 in 10 of its buses fell. Pick routes to compare them; All routes goes back to "
        f"every route together. {period[0].upper() + period[1:]}; an hour needs 5 arrivals to "
        "show. Hover a point for the numbers.",
    )
    hourly = q(
        f"""
        select route_id, route_short_name as route, direction_id, hour_local, count(*) as n,
               count(*) filter (where status = 'on_time') as on_time,
               percentile_cont(0.05) within group (order by delay_s) as p05,
               percentile_cont(0.1) within group (order by delay_s) as p10,
               percentile_cont(0.5) within group (order by delay_s) as p50,
               percentile_cont(0.9) within group (order by delay_s) as p90,
               count(distinct service_date) as n_days
        from marts.fct_stop_events
        where stop_id = %s and status is not null and service_date >= %s {wt_clause}
        group by 1, 2, 3, 4 order by 4
        """,
        (stop_id, start, *wt_params),
    )
    hourly["label"] = [
        labels.get((r, d), r) for r, d in zip(hourly["route"], hourly["direction_id"], strict=False)
    ]
    # every route here together, as one more line to choose
    ALL = "All routes"
    every = q(
        f"""
        select hour_local, count(*) as n,
               percentile_cont(0.05) within group (order by delay_s) as p05,
               percentile_cont(0.1) within group (order by delay_s) as p10,
               percentile_cont(0.5) within group (order by delay_s) as p50,
               percentile_cont(0.9) within group (order by delay_s) as p90,
               count(distinct service_date) as n_days
        from marts.fct_stop_events
        where stop_id = %s and status is not null and service_date >= %s {wt_clause}
        group by 1
        """,
        (stop_id, start, *wt_params),
    ).assign(route_id="__all__", route=ALL, direction_id=-1, label=ALL)
    hourly = pd.concat([hourly, every], ignore_index=True)
    options = [ALL, *tbl["label"].tolist()]
    pick_key = f"stop_hour_routes_{stop_id}"

    def _pick_lines() -> None:
        """All routes on its own, or any set of routes: picking a route replaces All routes,
        picking All routes replaces the routes, and nothing picked means All routes."""
        prev = st.session_state.get(f"_{pick_key}", [ALL])
        cur = list(st.session_state.get(pick_key) or [])
        if not cur or (ALL in cur and ALL not in prev):
            cur = [ALL]
        elif ALL in cur:
            cur = [c for c in cur if c != ALL]
        st.session_state[pick_key] = cur
        st.session_state[f"_{pick_key}"] = cur

    if pick_key not in st.session_state:
        st.session_state[pick_key] = [ALL]
    with st.container(key="rp_stop_lines"):  # styled like the other route buttons
        picked = st.pills(
            "Routes",
            options,
            selection_mode="multi",
            key=pick_key,
            on_change=_pick_lines,
            label_visibility="collapsed",
        )
    chart = hourly[hourly["label"].isin(picked or []) & (hourly["n"] >= 5)].copy()
    if not picked:
        st.caption("Pick a route above.")
    elif chart.empty:
        st.caption("Not enough arrivals yet for an hour-by-hour view (needs 5 in an hour).")
    else:
        colors = route_colors()
        hours = sorted(chart["hour_local"].unique(), key=service_hour_key)
        chart = chart.assign(_k=chart["hour_local"].map(service_hour_key)).sort_values("_k")
        x_of = {h: hour_label(h) for h in hours}
        fig = go.Figure()
        on_time_line(fig)
        one = len(picked) == 1
        for label in [o for o in options if o in picked]:
            d = chart[chart["label"] == label]
            if d.empty:
                continue
            color = "#1f5f9e" if label == ALL else colors.get(str(d["route_id"].iloc[0]), "#555555")
            x = [x_of[h] for h in d["hour_local"]]
            if one:
                # the range most buses fall in: 10th to 90th percentile
                fig.add_trace(
                    go.Scatter(
                        x=x + x[::-1],
                        y=list(d["p90"] / 60) + list(d["p10"] / 60)[::-1],
                        fill="toself",
                        fillcolor=color,
                        opacity=0.18,
                        mode="lines",
                        line_width=0,
                        hoverinfo="skip",
                        showlegend=False,
                    )
                )
            y = d["p50"] / 60
            hover = [
                (
                    f"<b>{label}</b>, {xx}<br>typical bus {fmt_delay(p50)}"
                    f"<br>8 in 10 buses: {fmt_range(p10, p90)}"
                    f"<br>{n:,} arrivals over {nd} days"
                )
                for xx, p10, p50, p90, n, nd in zip(
                    x, d["p10"], d["p50"], d["p90"], d["n"], d["n_days"], strict=False
                )
            ]
            fig.add_trace(
                go.Scatter(
                    x=x,
                    y=y,
                    name=label,
                    mode="lines+markers",
                    line={
                        "color": color,
                        "width": 2.5,
                        "dash": "dot"
                        if d["route"].iloc[0] in both_ways and d["direction_id"].iloc[0] == 1
                        else "solid",
                    },
                    hovertext=hover,
                    hoverinfo="text",
                )
            )
        fig.update_layout(
            xaxis={
                "type": "category",
                "categoryorder": "array",
                "categoryarray": [x_of[h] for h in hours],
            },
            yaxis=minutes_axis(
                list(chart["p50"] / 60)
                + (list(chart["p10"] / 60) + list(chart["p90"] / 60) if one else [])
            ),
            legend_title="",
            showlegend=not one,
            margin={"t": 30},
        )
        st.plotly_chart(fit_phone(fig), width="stretch")


# ---- how far off the countdown is here ------------------------------------------------
with card():
    st.subheader(
        "How far off is the countdown here?",
        help=COUNTDOWN_OFF_NOTE + f" {period[0].upper() + period[1:]}. More on the Countdown page.",
    )
    here_routes = tbl[["route_id", "route"]].drop_duplicates("route_id")
    fig = countdown_off_chart(
        countdown_off_at_stop(stop_id, start, wt),
        dict(
            zip(here_routes["route_id"].astype(str), here_routes["route"].astype(str), strict=False)
        ),
        focus_label="All routes here",
    )
    if fig is None:
        st.caption("Not enough measured arrivals with a countdown here yet for this period.")
    else:
        st.plotly_chart(fit_phone(fig), width="stretch")

# ---- arrive-by guidance ---------------------------------------------------
with card():
    st.subheader(
        "How early should I be at the stop?",
        help="Minutes before the scheduled time to be at the stop to catch 19 buses in 20 "
        "(0 = arriving on time is enough). Blank until a route has 20 arrivals here. "
        f"{period[0].upper() + period[1:]}.",
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
        guide = hourly[(hourly["n"] >= 10) & (hourly["label"] != ALL)].copy()
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
            order = ["Route " + o for o in options if o != ALL]
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
        "Your usual bus: each scheduled departure here",
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
    st.subheader(
        "Is it getting better?",
        help="One point per calendar week (Monday to Sunday), labelled with the days in it that "
        "have data. All routes and all days at this stop; the shaded band is where 8 in 10 buses "
        "fell.",
    )
    trend = q(
        """
        select date_trunc('week', service_date)::date as week, count(*) as n,
               min(service_date) as d0, max(service_date) as d1,
               percentile_cont(0.5) within group (order by delay_s) as median_delay,
               percentile_cont(0.1) within group (order by delay_s) as p10,
               percentile_cont(0.9) within group (order by delay_s) as p90
        from marts.fct_stop_events
        where stop_id = %s and status is not null group by 1 order by 1
        """,
        (stop_id,),
    )
    if len(trend) >= 2:
        for c in ("median_delay", "p10", "p90"):
            trend[c] = trend[c].astype(float) / 60

        def week_label(d0, d1) -> str:
            # the days that actually have data in that Monday-to-Sunday week: "Sep 26–27"
            a, b = fmt_date(d0), fmt_date(d1)
            if a == b:
                return a
            if a.split()[0] == b.split()[0]:
                return f"{a}–{b.split()[1]}"
            return f"{a}–{b}"

        x = [week_label(a, b) for a, b in zip(trend["d0"], trend["d1"], strict=False)]
        fig = go.Figure()
        on_time_line(fig)
        fig.add_trace(
            go.Scatter(
                x=x + x[::-1],
                y=list(trend["p90"]) + list(trend["p10"])[::-1],
                fill="toself",
                fillcolor="rgba(31,95,158,0.18)",
                mode="lines",
                line_width=0,
                hoverinfo="skip",
                name="8 in 10 buses",
            )
        )
        fig.add_trace(
            go.Scatter(
                x=x,
                y=trend["median_delay"],
                mode="lines+markers",
                name="Typical bus",
                line={"color": "#1f5f9e", "width": 2.5},
                hovertext=[
                    f"{xx}: typical bus {fmt_delay(m * 60)} · {int(n):,} arrivals"
                    for xx, m, n in zip(x, trend["median_delay"], trend["n"], strict=False)
                ],
                hoverinfo="text",
            )
        )
        fig.update_layout(
            xaxis={"type": "category", "title": ""},
            yaxis=minutes_axis(list(trend["p10"]) + list(trend["p90"])),
            legend={"orientation": "h", "yanchor": "bottom", "y": 1.02, "x": 0, "xanchor": "left"},
            margin={"t": 30},
            height=340,
        )
        st.plotly_chart(fit_phone(fig), width="stretch")
        st.caption("All data; doesn't follow the period and days filters.")
    else:
        st.caption("A trend needs data from at least two calendar weeks.")


st.divider()
data_note(start)
