"""Stops: how late buses run at all stops together, how reliable is my stop, and how early
should I get there?"""

from __future__ import annotations

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import pydeck as pdk
import streamlit as st
from common import (
    EARLY_HELP,
    JUST_LEFT_MINUTES,
    LATE_HELP,
    LATENESS_CHART_NOTE,
    LIVE_CHECK_SECONDS,
    STOP_ROWS_PER_ROUTE,
    TYPICAL_HELP,
    busy_stops,
    clean_headsigns,
    col_count,
    col_hour,
    col_late,
    col_minutes,
    col_pct,
    col_stop,
    col_typical,
    current_fv,
    data_note,
    day_label,
    day_sql,
    fit_phone,
    fmt_date,
    fmt_delay,
    fmt_pct,
    hour_label,
    hour_time,
    late_minutes,
    lateness,
    lateness_chart,
    live_status_line,
    marts_ready,
    page_filters,
    q,
    recomputing_note,
    require_db,
    require_marts,
    route_colors,
    search_stops,
    share_pct,
    show_coming,
    show_left,
    stop_buses,
    stop_buttons,
    stop_link,
    table,
)

require_db()
st.title("How reliable is my stop?")
require_marts()

FV = str(current_fv())  # schedule version in force today


# map colours for a stop's typical lateness (minutes behind the timetable)
LATENESS_BANDS = [
    (-1e9, -1, "#e6a100", "early (more than 1 min ahead)"),
    (-1, 2, "#2e8b57", "on schedule (1 min early to 2 late)"),
    (2, 5, "#e57373", "2 to 5 min late"),
    (5, 1e9, "#b71c1c", "more than 5 min late"),
]


def lateness_color(minutes: float) -> list[int]:
    for lo, hi, colour, _ in LATENESS_BANDS:
        if lo <= minutes < hi:
            return [int(colour[i : i + 2], 16) for i in (1, 3, 5)] + [220]
    return [150, 150, 150, 200]


def all_stops_summary() -> tuple:
    """Before a stop is chosen, at the top: how late buses run by hour at all stops together,
    and the headline numbers. Returns the filters and the per-stop rows for the rest."""
    st.markdown("#### When are buses late? All stops together")
    start, wt = page_filters()
    tot = lateness("all_stops", start, wt, "overall")
    if tot.empty:
        if marts_ready() and lateness("all_stops", start, None, "overall").empty:
            recomputing_note()
        else:
            st.info("No scored arrivals in the selected period yet.")
        return start, wt, pd.DataFrame()
    fig = lateness_chart(lateness("all_stops", start, wt, "hour"), "all stops")
    if fig is not None:
        st.plotly_chart(fit_phone(fig), width="stretch")
        st.caption(
            LATENESS_CHART_NOTE + f" Every stop, {day_label(wt)}. Choose a stop below to see "
            "its own, route by route."
        )
    t = tot.iloc[0]
    n = int(t["n"])
    per = lateness("all_stops", start, wt, "stop")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Typical bus, all stops", fmt_delay(t["median_delay_s"]), help=TYPICAL_HELP)
    c2.metric("Early (1+ min)", fmt_pct(int(t["n_early"]), n), help=EARLY_HELP)
    c3.metric("5+ min late", fmt_pct(int(t["n_late"]), n), help=LATE_HELP)
    c4.metric("Stops measured", f"{len(per):,}", f"{n:,} arrivals", delta_color="off")
    return start, wt, per


def all_stops_details(start, wt, per: pd.DataFrame) -> None:
    """Before a stop is chosen, below the pickers: a map of every stop coloured by its typical
    lateness (click one to open it), and the stops where buses are most often 5+ minutes late
    or early."""
    if per.empty:
        return
    period = f"{day_label(wt).capitalize()} since {fmt_date(start)}"
    names = q(f"""
        select stop_id, stop_name, stop_code, stop_lat as lat, stop_lon as lon
        from gtfs.stops where feed_version_id = {FV}
    """)
    per = per.merge(names, on="stop_id", how="inner").rename(columns={"routes_here": "routes"})
    per["typical"] = per["median_delay_s"] / 60
    per["early_pct"] = 100 * per["n_early"] / per["n"]
    per["late_pct"] = 100 * per["n_late"] / per["n"]

    st.markdown("#### Every stop on a map")
    shown = per[per["n"] >= 20].copy()
    shown["color"] = shown["typical"].map(lateness_color)
    shown["typical_txt"] = shown["typical"].map(lambda m: f"{m:+.1f} min")
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
        "Each dot is a stop, coloured by how late its typical bus is: "
        + " · ".join(f"{label}" for _, _, _, label in LATENESS_BANDS)
        + f" (amber, green, light red, dark red). {period}; stops with at least 20 arrivals. "
        "Click a stop to open it."
    )
    objs = (picked_map.selection.objects or {}).get("stops") if picked_map is not None else None
    clicked = objs[0]["stop_id"] if objs else None
    if clicked and clicked != st.session_state.get("stop_overview_last"):
        st.session_state["stop_overview_last"] = clicked
        st.session_state["stop_id"] = clicked
        st.rerun()

    worst = per[per["n"] >= 30]
    cols = {
        "Stop": col_stop("Stop"),
        "Typical bus": col_typical(max_minutes=max(5.0, float(worst["typical"].max() or 0))),
        "Early": col_pct("Early (1+ min)", help=EARLY_HELP),
        "Late": col_pct("5+ min late", help=LATE_HELP),
        "Arrivals": col_count("Arrivals"),
    }

    def stop_table(df: pd.DataFrame) -> None:
        table(
            pd.DataFrame(
                {
                    "Stop": [
                        stop_link(i, f"{nm} (#{c})" if c else nm)
                        for i, nm, c in zip(
                            df["stop_id"], df["stop_name"], df["stop_code"], strict=False
                        )
                    ],
                    "Routes": df["routes"].to_numpy(),
                    "Typical bus": df["typical"].to_numpy(),
                    "Early": df["early_pct"].to_numpy(),
                    "Late": df["late_pct"].to_numpy(),
                    "Arrivals": df["n"].astype(int).to_numpy(),
                }
            ),
            hide_index=True,
            width="stretch",
            column_config=cols,
        )

    st.markdown("**Where buses are most often 5+ minutes late**")
    stop_table(worst.sort_values(["late_pct", "n"], ascending=False).head(10))
    st.markdown("**Where buses most often come early** (when you could miss them)")
    stop_table(worst.sort_values(["early_pct", "n"], ascending=False).head(10))
    st.caption(f"{period}; stops with at least 30 arrivals. Click a stop's name to open it.")


def stop_button_grid(df: pd.DataFrame, key_prefix: str, show_code: bool = False) -> None:
    clicked = stop_buttons(df, key_prefix, show_code=show_code)
    if clicked:
        st.session_state["stop_id"] = clicked
        st.rerun()  # redraw with the stop pickers folded away


def stop_pickers(with_map: bool) -> None:
    """Busy-stop buttons, a list of every stop and (once a stop is chosen) a plain stop map;
    before a stop is chosen, the coloured all-stops map further down does the map's job."""
    busiest = busy_stops(9)
    st.caption("Start with a busy stop:")
    stop_button_grid(busiest, "busy")
    all_stops = q(f"""
        select stop_id, stop_code, stop_name from gtfs.stops
        where feed_version_id = {FV} and location_type = 0 order by stop_name
    """)
    all_stops["label"] = all_stops["stop_name"] + all_stops["stop_code"].map(
        lambda c: f"  ·  #{c}" if c else ""
    )
    label_to_id = dict(zip(all_stops["label"], all_stops["stop_id"], strict=False))

    def _picked_from_list() -> None:
        choice = st.session_state.get("stop_pick_list")
        if choice in label_to_id:
            st.session_state["stop_id"] = label_to_id[choice]

    st.selectbox(
        "Or pick any stop",
        ["—"] + all_stops["label"].tolist(),
        index=0,
        key="stop_pick_list",
        on_change=_picked_from_list,
    )
    if not with_map:
        return
    st.caption("Or pick a stop on the map:")
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

# ---- choose a stop ----------------------------------------------------------------
example = q(f"""
    select stop_code, stop_name from gtfs.stops
    where feed_version_id = {FV} and location_type = 0 and stop_code is not null and stop_code <> ''
    order by stop_name limit 1
""")
hint = (
    f"e.g. {example['stop_name'][0]}, or {example['stop_code'][0]}"
    if not example.empty
    else "stop name or number"
)
query = st.text_input("Search by stop name or the number on the sign", placeholder=hint)

if query:
    matches = search_stops(query)
    if matches.empty:
        st.warning("No stop matches that. Try part of a street name.")
    else:
        st.caption("Pick one:")
        stop_button_grid(matches, "stop", show_code=True)

stop_id = st.session_state.get("stop_id")
if not stop_id:
    # no stop yet: all stops together first, then ways to pick one, then the map and lists
    start, wt, per = all_stops_summary()
    if not query:
        st.markdown("#### Find your stop")
        stop_pickers(with_map=False)
    all_stops_details(start, wt, per)
    st.divider()
    data_note(start)
    st.stop()
if not query:
    with st.expander("Choose a different stop"):
        stop_pickers(with_map=True)
st.query_params["stop"] = stop_id
stop = q(
    f"select stop_id, stop_code, stop_name, stop_lat, stop_lon from gtfs.stops where stop_id = %s and feed_version_id = {FV}",
    (stop_id,),
).iloc[0]
st.subheader(f"{stop['stop_name']}" + (f"  ·  #{stop['stop_code']}" if stop["stop_code"] else ""))
st.page_link("views/map.py", label="What's coming to this stop right now →", icon="📍")


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
    st.caption(
        f"The next {n} buses of each route and direction, however far away, and any that left in "
        f"the last {JUST_LEFT_MINUTES} minutes. "
        "Arrives/In are LTD's predictions where it has one; LTD only predicts trips that are about "
        "to run, so later buses show the timetable. 'Likely in' corrects LTD's prediction by how far "
        "off it has usually been (same route, time of day, minutes ahead). Min late compares with "
        "the timetable; negative = early."
    )


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

# ---- when are buses late here: by hour, one line per route --------------------------------
st.markdown("#### When are buses late here?")
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
options = tbl["label"].tolist()
busiest = tbl.sort_values("n", ascending=False)["label"].head(6).tolist()
picked = st.pills(
    "Routes",
    options,
    selection_mode="multi",
    default=[o for o in options if o in busiest],
    key=f"stop_hour_routes_{stop_id}",
)
chart = hourly[hourly["label"].isin(picked or []) & (hourly["n"] >= 5)].copy()
if not picked:
    st.caption("Pick at least one route above.")
elif chart.empty:
    st.caption("Not enough arrivals yet for an hour-by-hour view (needs 5 in an hour).")
else:
    colors = route_colors()
    hours = sorted(chart["hour_local"].unique())
    x_of = {h: hour_label(h) for h in hours}
    fig = go.Figure()
    fig.add_hrect(
        y0=-1,
        y1=5,
        fillcolor="#0b6e4f",
        opacity=0.08,
        line_width=0,
        annotation_text="on time",
        annotation_position="top left",
    )
    fig.add_hline(y=0, line_width=1, line_color="#999", line_dash="dot")
    one = len(picked) == 1
    for label in [o for o in options if o in picked]:
        d = chart[chart["label"] == label]
        if d.empty:
            continue
        color = colors.get(str(d["route_id"].iloc[0]), "#0b6e4f")
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
                    line_width=0,
                    hoverinfo="skip",
                    showlegend=False,
                )
            )
        y = d["p50"] / 60
        hover = [
            (
                f"<b>{label}</b>, {xx}<br>typically {p50 / 60:+.1f} min"
                f"<br>8 in 10 buses: {p10 / 60:+.0f} to {p90 / 60:+.0f} min"
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
        yaxis_title="minutes late (typical bus)",
        yaxis_tickformat="+d",
        legend_title="",
        showlegend=not one,
        margin={"t": 30},
    )
    st.plotly_chart(fit_phone(fig), width="stretch")
    st.caption(
        "The line is the typical bus (the median) in each hour, in minutes behind the timetable; "
        "below zero = early. The green band is on time (up to 1 min early, 5 min late)."
        + (
            " The shaded range is where 8 in 10 buses fell."
            if one
            else " Pick one route to see the range where most of its buses fall."
        )
        + f" Scheduled hour, {period}; an hour needs 5 arrivals to show. Hover a point for details."
    )

# ---- per route ------------------------------------------------------------------------------
st.markdown("**Routes at this stop**")
total_n = int(by_route["n"].sum())
here = q(
    f"""
    select percentile_cont(0.5) within group (order by delay_s) as median_delay
    from marts.fct_stop_events
    where stop_id = %s and status is not null and service_date >= %s {wt_clause}
    """,
    (stop_id, start, *wt_params),
)
c1, c2, c3, c4 = st.columns(4)
c1.metric("Typical bus here", fmt_delay(here["median_delay"][0]), help=TYPICAL_HELP)
c2.metric("Early (1+ min)", fmt_pct(int(by_route["early"].sum()), total_n), help=EARLY_HELP)
c3.metric("5+ min late", fmt_pct(int(by_route["late"].sum()), total_n), help=LATE_HELP)
c4.metric(
    "Scored arrivals",
    f"{total_n:,}",
    f"{int(by_route['n_days'].max())} day(s), {fmt_date(by_route['first_day'].min())}–{fmt_date(by_route['last_day'].max())}",
    delta_color="off",
)
table(
    pd.DataFrame(
        {
            "Route": tbl["route"].to_numpy(),
            "Toward": tbl["headsign"].fillna("").to_numpy(),
            "Typical bus": late_minutes(tbl["median_delay"]).to_numpy(),
            "Early": share_pct(tbl["early"], tbl["n"]).to_numpy(),
            "Late": share_pct(tbl["late"], tbl["n"]).to_numpy(),
            "Arrivals": tbl["n"].astype(int).to_numpy(),
        }
    ),
    hide_index=True,
    width="stretch",
    column_config={
        "Typical bus": col_typical(
            max_minutes=max(5.0, float(late_minutes(tbl["median_delay"]).max() or 0))
        ),
        "Early": col_pct("Early (1+ min)", help=EARLY_HELP),
        "Late": col_pct("5+ min late", help=LATE_HELP),
        "Arrivals": col_count("Arrivals"),
    },
)

# ---- arrive-by guidance ---------------------------------------------------
st.markdown("**How early should I be at the stop?**")
early_all = q(
    f"""
    select route_short_name as route, direction_id, count(*) as n,
           percentile_cont(0.05) within group (order by delay_s) as p05
    from marts.fct_stop_events
    where stop_id = %s and status is not null and service_date >= %s {wt_clause}
    group by 1, 2
    """,
    (stop_id, start, *wt_params),
)
early_all = early_all.merge(hs, on=["route", "direction_id"], how="left")
early_all["rkey"] = early_all["route"].map(route_key)
early_all = early_all.sort_values(["rkey", "direction_id"])
enough = early_all["n"] >= 20
table(
    pd.DataFrame(
        {
            "Route": early_all["route"],
            "Toward": early_all["headsign"].fillna(""),
            "Be there early by": [
                (0.0 if s >= -30 else round(-float(s) / 60)) if ok else None
                for s, ok in zip(early_all["p05"], enough, strict=False)
            ],
            "Arrivals": early_all["n"],
        }
    ),
    hide_index=True,
    width="content",
    column_config={
        "Be there early by": col_minutes("Be there early by", fmt="%.0f min"),
        "Arrivals": col_count("Arrivals"),
    },
)
st.caption(
    "Minutes before the scheduled time to be at the stop to catch 19 buses in 20 "
    "(0 = on time is enough). Blank until a route has 20 arrivals here."
)
with st.expander("Hour by hour"):
    guide = hourly[hourly["n"] >= 10].copy()
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
        order = ["Route " + o for o in options]
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
        st.caption(
            f"Same measure for each hour, {period}. Blank where an hour has fewer than 10 arrivals."
        )

# ---- right now ------------------------------------------------------------------------------
st.markdown("#### Right now at this stop")
stop_right_now()

# ---- trend --------------------------------------------------------------------
st.markdown("**Is it getting better?**")
trend = q(
    """
    select date_trunc('week', service_date)::date as week, count(*) as n,
           count(*) filter (where status = 'early') as early,
           count(*) filter (where status = 'late') as late,
           percentile_cont(0.5) within group (order by delay_s) as median_delay
    from marts.fct_stop_events
    where stop_id = %s and status is not null group by 1 order by 1
    """,
    (stop_id,),
)
if len(trend) >= 2:
    trend["Typical bus"] = trend["median_delay"].astype(float) / 60
    trend["Week of"] = trend["week"].map(fmt_date)
    trend["Early (1+ min)"] = trend["early"] / trend["n"]
    trend["5+ min late"] = trend["late"] / trend["n"]
    trend["Arrivals"] = trend["n"].astype(int)
    fig = px.line(
        trend,
        x="Week of",
        y="Typical bus",
        markers=True,
        hover_data={
            "Typical bus": ":+.1f",
            "Early (1+ min)": ":.0%",
            "5+ min late": ":.0%",
            "Arrivals": True,
        },
    )
    fig.add_hline(y=0, line_width=1, line_color="#999", line_dash="dot")
    fig.update_layout(
        xaxis_title="", yaxis_title="typical bus, minutes late", yaxis_tickformat="+.1f"
    )
    st.plotly_chart(fit_phone(fig), width="stretch")
    st.caption("The typical bus here each week, all routes and days; below zero = early.")
else:
    st.caption("A trend needs at least two weeks of data.")

# ---- nearby stops ---------------------------------------------------------------
st.markdown("**Nearby stops where the same route keeps better time**")
near = q(
    f"""
    with here as (select geom from gtfs.stops where stop_id = %s and feed_version_id = {FV}),
    mine as (
        select route_id, sum(n_early + n_late)::float / nullif(sum(n_events), 0) as off
        from marts.mart_stop_route_daily where stop_id = %s and service_date >= %s group by 1
    )
    select s.stop_id, s.stop_name, s.stop_code, round(ST_Distance(s.geom, here.geom)) as metres,
           m.route_short_name as route,
           sum(m.n_early + m.n_late)::float / nullif(sum(m.n_events), 0) as off, mine.off as off_here
    from gtfs.stops s
    cross join here
    join marts.mart_stop_route_daily m on m.stop_id = s.stop_id
    join mine on mine.route_id = m.route_id
    where s.feed_version_id = {FV} and s.stop_id <> %s and ST_DWithin(s.geom, here.geom, 500)
      and m.service_date >= %s
    group by 1, 2, 3, 4, 5, mine.off
    having sum(m.n_events) >= 20
       and sum(m.n_early + m.n_late)::float / sum(m.n_events) < mine.off - 0.05
    order by off limit 8
    """,
    (stop_id, stop_id, start, stop_id, start),
)
if near.empty:
    st.caption("None within 500 m on the same routes — this stop is as good as its neighbours.")
else:
    table(
        pd.DataFrame(
            {
                "Stop": [
                    stop_link(i, f"{n} (#{c})" if c else n)
                    for i, n, c in zip(
                        near["stop_id"], near["stop_name"], near["stop_code"], strict=False
                    )
                ],
                "Distance": near["metres"].astype(float),
                "Route": near["route"],
                "There": 100 * near["off"].astype(float),
                "Here": 100 * near["off_here"].astype(float),
            }
        ),
        hide_index=True,
        width="stretch",
        column_config={
            "Stop": col_stop("Stop"),
            "Distance": col_minutes("Distance", fmt="%.0f m"),
            "There": col_pct("Early or 5+ late there", bar=True),
            "Here": col_pct("Early or 5+ late here", bar=True),
        },
    )
    st.caption(
        "Stops within 500 m where the same route's buses were at least 5 points less often early "
        f"(1+ min) or 5+ min late than here, all days since {fmt_date(start)}."
    )

with st.expander("Details"):
    st.write(
        f"Stop id `{stop_id}` · location {stop['stop_lat']:.5f}, {stop['stop_lon']:.5f} · "
        f"arrivals counted: {day_label(wt)} since {fmt_date(start)}"
    )
    table(
        pd.DataFrame(
            {
                "Route": by_route["route"],
                "Direction": by_route["direction_id"],
                "Arrivals": by_route["n"],
                "On time": by_route["on_time"],
                "Early": by_route["early"],
                "Late": by_route["late"],
                "Typical bus (min late)": late_minutes(by_route["median_delay"]),
                "First day": by_route["first_day"],
                "Last day": by_route["last_day"],
                "Days": by_route["n_days"],
            }
        ),
        hide_index=True,
        width="stretch",
        column_config={"Typical bus (min late)": col_late("Typical bus (min late)")},
    )

st.divider()
data_note(start)
