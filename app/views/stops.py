"""Stops: how reliable is my stop, and how early should I get there?"""

from __future__ import annotations

import pandas as pd
import plotly.express as px
import pydeck as pdk
import streamlit as st
from common import (
    LIVE_CHECK_SECONDS,
    add_honest_columns,
    busy_stops,
    clean_headsigns,
    col_ago,
    col_count,
    col_hour,
    col_late,
    col_likely,
    col_minutes,
    col_pct,
    col_stop,
    col_time,
    col_usual,
    current_fv,
    data_note,
    data_now,
    day_label,
    day_sql,
    empty_message,
    fmt_date,
    fmt_pct,
    hour_label,
    hour_time,
    late_minutes,
    live_marker,
    live_status_line,
    local_times,
    page_filters,
    q,
    q_fresh,
    require_db,
    require_marts,
    schedule_join,
    search_stops,
    share_pct,
    stop_buttons,
    stop_link,
    table,
    weekday_type_label,
)

require_db()
st.title("How reliable is my stop?")
require_marts()
start, wt = page_filters()

FV = str(current_fv())  # schedule version in force today


def stop_button_grid(df: pd.DataFrame, key_prefix: str, show_code: bool = False) -> None:
    clicked = stop_buttons(df, key_prefix, show_code=show_code)
    if clicked:
        st.session_state["stop_id"] = clicked


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
else:
    busiest = busy_stops(9)
    st.caption("Or start with a busy stop:")
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
    with st.expander("Or pick a stop on the map"):
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

if "stop" in st.query_params and not st.session_state.get("stop_id"):
    st.session_state["stop_id"] = st.query_params["stop"]
stop_id = st.session_state.get("stop_id")
if not stop_id:
    st.stop()
st.query_params["stop"] = stop_id
stop = q(
    f"select stop_id, stop_code, stop_name, stop_lat, stop_lon from gtfs.stops where stop_id = %s and feed_version_id = {FV}",
    (stop_id,),
).iloc[0]
st.subheader(f"{stop['stop_name']}" + (f"  ·  #{stop['stop_code']}" if stop["stop_code"] else ""))
st.page_link("views/live.py", label="What's coming to this stop right now →", icon="📍")


# ---- right now at this stop ------------------------------------------------------------
@st.fragment(run_every=f"{LIVE_CHECK_SECONDS}s")
def stop_right_now() -> None:
    sj, sched = schedule_join("p")
    live_status_line()
    rows = q_fresh(
        f"""
        select r.route_short_name as route, t.route_id, t.trip_headsign as headsign,
               coalesce(stt.timepoint, case when stt.arrival_seconds is null then 0 else 1 end) = 1 as is_timepoint,
               coalesce(p.arrival_time, p.departure_time) as t, {sched} as scheduled,
               extract(epoch from coalesce(p.arrival_time, p.departure_time) - {sched})::int as delay_s,
               p.last_seen_at >= coalesce(p.arrival_time, p.departure_time) + interval '20 seconds' as departed
        from rt.prediction_current p
        join gtfs.trips t  on t.trip_id = p.trip_id and t.feed_version_id = {FV}
        join gtfs.routes r on r.route_id = t.route_id and r.feed_version_id = {FV}
        left join gtfs.stop_times stt on stt.feed_version_id = {FV} and stt.trip_id = p.trip_id
                                     and stt.stop_sequence = p.stop_sequence
        {sj}
        where p.stop_id = %s
          and p.last_seen_at > now() - interval '3 minutes'
          and coalesce(p.arrival_time, p.departure_time) between now() - interval '30 minutes' and now() + interval '60 minutes'
        order by coalesce(p.arrival_time, p.departure_time)
        """,
        (stop_id,),
        marker=live_marker()["fid"],
    )
    now = data_now()
    rows["t"] = pd.to_datetime(rows["t"], utc=True)
    coming = rows[rows["t"] >= now - pd.Timedelta(seconds=30)].copy()
    coming["mins"] = ((coming["t"] - now).dt.total_seconds() / 60).clip(lower=0).round()
    coming = add_honest_columns(coming, "mins", "route_id", "is_timepoint")
    left = rows[(rows["t"] < now) & rows["departed"].astype(bool)].sort_values("t", ascending=False)
    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**Coming up (next 60 minutes)**")
        if coming.empty:
            st.caption(empty_message("No buses predicted for this stop in the next 60 minutes."))
        else:
            table(
                pd.DataFrame(
                    {
                        "Route": coming["route"],
                        "Toward": clean_headsigns(coming),
                        "Arrives": local_times(coming["t"]),
                        "In": coming["mins"],
                        "Likely in": coming["likely_min"],
                        "80% of the time": coming["usual_range"],
                        "Scheduled": local_times(coming["scheduled"]),
                        "Min late": late_minutes(coming["delay_s"]),
                    }
                ),
                hide_index=True,
                width="stretch",
                column_config={
                    "Arrives": col_time("Arrives"),
                    "In": col_minutes("In", help="Minutes until LTD's predicted time (0 = now)."),
                    "Likely in": col_likely(),
                    "80% of the time": col_usual(),
                    "Scheduled": col_time("Scheduled"),
                    "Min late": col_late(),
                },
            )
    with c2:
        st.markdown("**Just left (last 30 minutes)**")
        if left.empty:
            st.caption("No departures from this stop recorded in the last 30 minutes.")
        else:
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
                    "Left at": col_time("Left at"),
                    "Scheduled": col_time("Scheduled"),
                    "Min late": col_late(),
                    "Ago": col_ago(),
                },
            )
    st.caption(
        "Arrives/In are LTD's predictions; 'Likely in' corrects them by how far off LTD has usually been "
        "(same route, time of day, minutes ahead). Min late compares with the timetable; negative = early."
    )


with st.expander("Where is this stop?", expanded=False):
    st.map(
        pd.DataFrame({"lat": [float(stop["stop_lat"])], "lon": [float(stop["stop_lon"])]}),
        zoom=15,
        size=25,
        height=260,
    )

st.markdown("#### Right now at this stop")
stop_right_now()

wt_clause, wt_params = day_sql(wt)

# ---- per route ----------------------------------------------------------------
by_route = q(
    f"""
    select route_short_name as route, direction_id, count(*) as n,
           count(*) filter (where status = 'on_time') as on_time,
           count(*) filter (where status = 'early') as early,
           count(*) filter (where status = 'late') as late,
           percentile_cont(0.5) within group (order by delay_s) as median_delay,
           min(service_date) as first_day, max(service_date) as last_day,
           count(distinct service_date) as n_days
    from marts.fct_stop_events
    where stop_id = %s and status is not null and service_date >= %s {wt_clause}
    group by 1, 2 order by 1, 2
    """,
    (stop_id, start, *wt_params),
)
if by_route.empty:
    st.info("No scored arrivals at this stop in the selected period yet.")
    st.stop()

total_n, total_ot = int(by_route["n"].sum()), int(by_route["on_time"].sum())
c1, c2, c3 = st.columns(3)
c1.metric(
    "On time here", fmt_pct(total_ot, total_n), help="No more than 1 min early or 5 min late."
)
c2.metric(
    "Ran early",
    fmt_pct(int(by_route["early"].sum()), total_n),
)
c3.metric(
    "Scored arrivals",
    f"{total_n:,}",
    f"{int(by_route['n_days'].max())} day(s), {fmt_date(by_route['first_day'].min())}–{fmt_date(by_route['last_day'].max())}",
    delta_color="off",
)

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
st.markdown("**Routes at this stop**")
table(
    pd.DataFrame(
        {
            "Route": tbl["route"].to_numpy(),
            "Toward": tbl["headsign"].fillna("").to_numpy(),
            "On time": share_pct(tbl["on_time"], tbl["n"]).to_numpy(),
            "Early": share_pct(tbl["early"], tbl["n"]).to_numpy(),
            "Late": share_pct(tbl["late"], tbl["n"]).to_numpy(),
            "Typical bus": late_minutes(tbl["median_delay"]).to_numpy(),
            "Arrivals": tbl["n"].astype(int).to_numpy(),
        }
    ),
    hide_index=True,
    width="stretch",
    column_config={
        "On time": col_pct("On time", bar=True),
        "Early": col_pct("Early"),
        "Late": col_pct("Late"),
        "Typical bus": col_late(
            "Typical bus (min late)", help="Median minutes late; negative = early."
        ),
        "Arrivals": col_count("Arrivals"),
    },
)

# ---- by hour ------------------------------------------------------------------
st.markdown("**When is it worst?**")
hourly = q(
    f"""
    select route_short_name as route, weekday_type, hour_local, count(*) as n,
           count(*) filter (where status = 'on_time') as on_time,
           count(*) filter (where status = 'early') as early,
           count(*) filter (where status = 'late') as late,
           percentile_cont(0.5) within group (order by delay_s) as median_delay,
           percentile_cont(0.05) within group (order by delay_s) as p05,
           min(service_date) as first_day, max(service_date) as last_day,
           count(distinct service_date) as n_days
    from marts.fct_stop_events
    where stop_id = %s and status is not null {wt_clause}
    group by 1, 2, 3 order by 1, 2, 3
    """,
    (stop_id, *wt_params),
)
if not hourly.empty:
    hourly["Hour"] = hourly["hour_local"].map(hour_label)
    hourly["On time"] = hourly["on_time"] / hourly["n"]
    hourly["Days"] = hourly["weekday_type"].map(weekday_type_label)
    hourly["Arrivals"] = hourly["n"].astype(int)
    hourly["Days of data"] = hourly["n_days"].astype(int)
    hourly["From"] = hourly["first_day"].map(fmt_date)
    hourly["To"] = hourly["last_day"].map(fmt_date)
    fig = px.line(
        hourly,
        x="Hour",
        y="On time",
        color="route",
        line_dash="Days",
        markers=True,
        hover_data={
            "Arrivals": True,
            "Days of data": True,
            "From": True,
            "To": True,
            "Hour": False,
            "On time": ":.0%",
        },
        category_orders={"Hour": [hour_label(h) for h in range(24)]},
    )
    fig.update_layout(yaxis_tickformat=".0%", xaxis_title="", legend_title="Route")
    st.plotly_chart(fig, width="stretch")
    st.caption(
        "Share of arrivals on time by hour of day, all collected days (the period filter doesn't apply here). Hover a point for sample size and dates."
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
    if not early_all.empty:
        early_all = early_all.merge(hs, on=["route", "direction_id"], how="left")
        early_all["rkey"] = early_all["route"].astype(str).map(lambda v: (len(v), v))
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
        "(0 = on time is enough). Blank until a route has 20 arrivals here; the hour-by-hour table "
        "needs 10 in an hour, so it fills in as data builds up."
    )
    guide = hourly[hourly["n"] >= 10].copy()
    # minutes before the scheduled time; 0 when the earliest buses are no more than 30 s early
    guide["early_by"] = guide["p05"].map(
        lambda s: 0.0 if s is None or pd.isna(s) or s >= -30 else round(-float(s) / 60)
    )
    if guide.empty:
        st.caption("Not enough arrivals per hour yet (needs 10+ per hour).")
    else:
        guide["col"] = "Route " + guide["route"].astype(str) + " · " + guide["Days"]
        pivot = guide.pivot_table(
            index="hour_local", columns="col", values="early_by", aggfunc="first"
        ).sort_index()
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

# ---- trend --------------------------------------------------------------------
st.markdown("**Is it getting better?**")
trend = q(
    """
    select date_trunc('week', service_date)::date as week, sum(n_events) as n, sum(n_on_time) as on_time
    from marts.mart_stop_route_daily where stop_id = %s group by 1 order by 1
    """,
    (stop_id,),
)
if len(trend) >= 2:
    trend["On time"] = trend["on_time"] / trend["n"]
    trend["Week of"] = trend["week"].map(fmt_date)
    trend["Arrivals"] = trend["n"].astype(int)
    fig = px.line(
        trend,
        x="Week of",
        y="On time",
        markers=True,
        hover_data={"Arrivals": True, "On time": ":.0%"},
    )
    fig.update_layout(yaxis_tickformat=".0%", xaxis_title="")
    st.plotly_chart(fig, width="stretch")
else:
    st.caption("A trend needs at least two weeks of data.")

# ---- nearby stops ---------------------------------------------------------------
st.markdown("**Nearby stops that do better**")
near = q(
    f"""
    with here as (select geom from gtfs.stops where stop_id = %s and feed_version_id = {FV}),
    mine as (
        select route_id, sum(n_on_time)::float / nullif(sum(n_events), 0) as on_time
        from marts.mart_stop_route_daily where stop_id = %s and service_date >= %s group by 1
    )
    select s.stop_id, s.stop_name, s.stop_code, round(ST_Distance(s.geom, here.geom)) as metres,
           m.route_short_name as route,
           sum(m.n_on_time)::float / nullif(sum(m.n_events), 0) as on_time, mine.on_time as on_time_here
    from gtfs.stops s
    cross join here
    join marts.mart_stop_route_daily m on m.stop_id = s.stop_id
    join mine on mine.route_id = m.route_id
    where s.feed_version_id = {FV} and s.stop_id <> %s and ST_DWithin(s.geom, here.geom, 500)
      and m.service_date >= %s
    group by 1, 2, 3, 4, 5, mine.on_time
    having sum(m.n_events) >= 20 and sum(m.n_on_time)::float / sum(m.n_events) > mine.on_time + 0.05
    order by on_time desc limit 8
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
                "On time there": 100 * near["on_time"].astype(float),
                "On time here": 100 * near["on_time_here"].astype(float),
            }
        ),
        hide_index=True,
        width="stretch",
        column_config={
            "Stop": col_stop("Stop"),
            "Distance": col_minutes("Distance", fmt="%.0f m"),
            "On time there": col_pct("On time there", bar=True),
            "On time here": col_pct("On time here", bar=True),
        },
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
