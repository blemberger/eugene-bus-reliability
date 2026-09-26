"""Live: every bus with heading and next stops; next arrivals at any stop; alerts.

The map is pydeck on Streamlit's built-in basemap; it refreshes itself every 15 s.
"""

from __future__ import annotations

import base64
import math

import pandas as pd
import pydeck as pdk
import streamlit as st
from common import (
    LIVE_CHECK_SECONDS,
    STATUS_COLOR,
    STATUS_LABEL,
    STOP_ROWS_PER_ROUTE,
    busy_stop_buttons,
    clean_headsigns,
    col_ago,
    col_late,
    col_minutes,
    col_time,
    current_fv,
    data_now,
    empty_message,
    fmt_ago,
    fmt_delay,
    fmt_time,
    hex_to_rgb,
    is_mobile,
    late_minutes,
    live_marker,
    live_status_line,
    local_times,
    q,
    q_fresh,
    require_db,
    route_picker,
    schedule_join,
    search_stops,
    show_coming,
    status_from_delay,
    stop_buses,
    stop_buttons,
    table,
)

require_db()
st.title("Where are the buses right now?")

OCCUPANCY = {
    0: "empty",
    1: "many seats",
    2: "few seats",
    3: "standing room",
    4: "crushed",
    5: "full",
    6: "not accepting",
}
FV = str(current_fv())  # schedule version in force today


BUS_DOT_PX = 7
STOP_DOT_PX = 5  # the red next-stop markers
HALO_PX = 15  # the ring around a selected bus
ARROW_ICON_PX = 44
_ARROW_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" viewBox="0 0 64 64">'
    '<path d="M32 20 L32 8" stroke="black" stroke-width="6" stroke-linecap="butt"/>'
    '<path d="M32 0 L24.5 9.5 L39.5 9.5 Z" fill="black"/></svg>'
)
ARROW_ICON = {
    "url": "data:image/svg+xml;base64," + base64.b64encode(_ARROW_SVG.encode()).decode(),
    "width": 64,
    "height": 64,
    "anchorY": 32,
    "mask": True,
}


def load_live() -> pd.DataFrame:
    sj, sched = schedule_join("p")
    return q_fresh(
        f"""
        with latest as (
            select distinct on (vehicle_id) *
            from rt.vehicle_position
            where position_timestamp between now() - interval '10 minutes' and now() + interval '5 minutes'
            order by vehicle_id, position_timestamp desc
        )
        select vp.vehicle_id, vp.route_id, r.route_short_name as route, t.trip_headsign as headsign,
               vp.latitude as lat, vp.longitude as lon, vp.bearing, vp.speed_mps, vp.occupancy_status,
               vp.position_timestamp,
               s.stop_name as next_stop, s.stop_lat as next_lat, s.stop_lon as next_lon,
               coalesce(p.arrival_time, p.departure_time) as next_time,
               s2.stop_name as stop2, s2.stop_lat as lat2s, s2.stop_lon as lon2s,
               coalesce(p2.arrival_time, p2.departure_time) as time2,
               coalesce(p.arrival_delay, p.departure_delay,
                        extract(epoch from coalesce(p.arrival_time, p.departure_time) - {sched})::int) as delay_s
        from latest vp
        left join gtfs.trips t  on t.trip_id = vp.trip_id and t.feed_version_id = {FV}
        left join gtfs.routes r on r.route_id = t.route_id and r.feed_version_id = t.feed_version_id
        left join lateral (
            select * from rt.prediction_current x
            where x.trip_id = vp.trip_id and x.start_date = vp.start_date
              and coalesce(x.arrival_time, x.departure_time) >= vp.position_timestamp - interval '30 seconds'
            order by x.stop_sequence limit 1
        ) p on true
        left join gtfs.stops s  on s.stop_id = p.stop_id and s.feed_version_id = t.feed_version_id
        left join lateral (
            select * from rt.prediction_current x
            where x.trip_id = vp.trip_id and x.start_date = vp.start_date and x.stop_sequence > p.stop_sequence
            order by x.stop_sequence limit 1
        ) p2 on true
        left join gtfs.stops s2 on s2.stop_id = p2.stop_id and s2.feed_version_id = t.feed_version_id
        {sj}
        order by vp.vehicle_id
    """,
        marker=live_marker()["fid"],
    )


@st.fragment(run_every=f"{LIVE_CHECK_SECONDS}s")
def live_map() -> None:
    live_status_line()
    map_h = 420 if is_mobile() else 560
    live = load_live()
    if live.empty:
        st.info(
            empty_message(
                "No buses reporting in the last 10 minutes. Between midnight and 5 am that's normal."
            )
        )
        return

    live["status"] = live["delay_s"].map(status_from_delay)
    live["color"] = live["status"].map(lambda k: hex_to_rgb(STATUS_COLOR[k]))
    live["route"] = live["route"].fillna(live["route_id"]).astype(str)
    live = live.assign(
        running=live["delay_s"].map(
            lambda d: (
                "Lateness not known yet"
                if d is None or pd.isna(d)
                else f"Running {fmt_delay(d)} (timetable vs LTD's prediction for its next stop)"
            )
        ),
        speed=live["speed_mps"].map(lambda v: "" if pd.isna(v) else f"Speed {v * 2.237:.0f} mph"),
        reported=live["position_timestamp"].map(lambda t: fmt_ago(t, now=data_now())),
        next_at=live["next_time"].map(fmt_time),
        then=live["stop2"].fillna("—"),
        then_at=live["time2"].map(fmt_time),
        load=live["occupancy_status"].map(
            lambda o: "" if pd.isna(o) else f" · Seats: {OCCUPANCY.get(int(o), '')}"
        ),
        headsign=clean_headsigns(live),
        next_stop=live["next_stop"].fillna("—"),
        stop_line="",  # filled in for stop markers only; every tooltip field must exist on every layer
    )
    live["label"] = (
        "Route "
        + live["route"]
        + " → "
        + live["headsign"]
        + " · bus "
        + live["vehicle_id"].astype(str)
    )

    # ---- selection: a route (chips) and optionally one bus on it (dropdown) ----
    try:
        objs = st.session_state["live_map"]["selection"]["objects"].get("buses") or []
        clicked_vid = objs[0].get("vehicle_id") if objs else None
    except (KeyError, TypeError, AttributeError):
        clicked_vid = None
    if not clicked_vid:  # nothing selected on the map: the next click on any bus counts
        st.session_state["live_click_last"] = None
    if clicked_vid and clicked_vid != st.session_state.get("live_click_last"):
        st.session_state["live_click_last"] = clicked_vid
        bus = live[live["vehicle_id"] == clicked_vid]
        if not bus.empty:
            st.session_state["live_route"] = str(
                bus["route_id"].fillna(bus["route"]).astype(str).iloc[0]
            )
            st.session_state["live_bus_pick"] = bus["label"].iloc[0]

    st.write(
        "Pick a route (or click a bus on the map) to see the route and its buses' next stops; "
        "pick a bus to zoom to it."
    )
    present = (
        live.assign(route_id=live["route_id"].fillna(live["route"]).astype(str))[
            ["route_id", "route"]
        ]
        .drop_duplicates("route_id")
        .rename(columns={"route": "route_short_name"})
    )
    sel_route = route_picker(present, key="live_route")
    shown = live[live["route_id"].astype(str) == sel_route] if sel_route else live

    sel_bus = None
    if sel_route:
        options = ["(all buses on this route)"] + shown.sort_values("label")["label"].tolist()
        if st.session_state.get("live_bus_pick") not in options:  # that bus left the route
            st.session_state["live_bus_pick"] = options[0]
        choice = st.selectbox("Zoom to a bus", options, key="live_bus_pick")
        if choice != options[0] and choice in shown["label"].values:
            sel_bus = shown[shown["label"] == choice].iloc[0]

    # ---- geometry ----
    arrows = shown[shown["bearing"].notna()].copy()
    arrows["icon"] = [ARROW_ICON] * len(arrows)
    arrows["angle"] = -arrows["bearing"].astype(float)  # deck.gl turns counter-clockwise
    focus = shown if sel_route else shown.iloc[0:0]  # next-stop lines only when a route is chosen
    if sel_bus is not None:
        focus = shown[shown["vehicle_id"] == sel_bus["vehicle_id"]]
    highlighted = sel_bus is not None

    # the route's own path under the buses, when a route is chosen
    route_paths: list[dict] = []
    if sel_route:
        pts = q(
            f"""
            select s.shape_id, s.shape_pt_lon as lon, s.shape_pt_lat as lat
            from gtfs.shapes s
            where s.feed_version_id = {FV}
              and s.shape_id in (select distinct shape_id from gtfs.trips
                                 where route_id = %s and feed_version_id = {FV})
            order by s.shape_id, s.shape_pt_sequence
            """,
            (sel_route,),
        )
        route_paths = [
            {"path": g[["lon", "lat"]].to_numpy().tolist()} for _, g in pts.groupby("shape_id")
        ]
    to_stop = focus.dropna(subset=["next_lat", "next_lon"])
    to_then = focus.dropna(subset=["lat2s", "lon2s", "next_lat", "next_lon"])
    stops_pts = (
        pd.concat(
            [
                to_stop.assign(
                    slat=to_stop["next_lat"],
                    slon=to_stop["next_lon"],
                    sname=to_stop["next_stop"],
                    sat=to_stop["next_at"],
                    which="Next stop",
                ),
                to_then.assign(
                    slat=to_then["lat2s"],
                    slon=to_then["lon2s"],
                    sname=to_then["then"],
                    sat=to_then["then_at"],
                    which="Then",
                ),
            ]
        )
        if len(focus)
        else focus.iloc[0:0]
    )
    if len(stops_pts):
        stops_pts = stops_pts.assign(
            stop_line="<i>"
            + stops_pts["which"]
            + ": "
            + stops_pts["sname"]
            + " at "
            + stops_pts["sat"]
            + "</i><br/>"
        )

    layers = [
        pdk.Layer(
            "PathLayer",
            id="route_path",
            data=route_paths,
            get_path="path",
            get_color=[40, 90, 160, 55],  # faint: the buses and their lines stay in front
            get_width=4,
            width_min_pixels=3,
            width_max_pixels=5,
            pickable=False,
        ),
        pdk.Layer(
            "LineLayer",
            data=to_then,
            get_source_position="[next_lon, next_lat]",
            get_target_position="[lon2s, lat2s]",
            get_color=[230, 30, 30, 150],
            get_width=2,
            pickable=False,
        ),
        pdk.Layer(
            "LineLayer",
            data=to_stop,
            get_source_position="[lon, lat]",
            get_target_position="[next_lon, next_lat]",
            get_color=[230, 30, 30, 220],
            get_width=3,
            pickable=False,
        ),
        pdk.Layer(
            "ScatterplotLayer",
            data=stops_pts,
            get_position="[slon, slat]",
            get_fill_color=[230, 30, 30, 230],
            get_radius=26,
            radius_min_pixels=STOP_DOT_PX,  # min = max: fixed pixel size, like the buses
            radius_max_pixels=STOP_DOT_PX,
            pickable=True,
        ),
        pdk.Layer(
            "IconLayer",
            data=arrows,
            get_icon="icon",
            get_position="[lon, lat]",
            get_angle="angle",
            get_color="color",
            get_size=ARROW_ICON_PX,  # in screen pixels (IconLayer's default unit)
            pickable=False,
        ),
        pdk.Layer(
            "ScatterplotLayer",
            id="buses",
            data=shown,
            get_position="[lon, lat]",
            get_fill_color="color",
            get_line_color=[255, 255, 255],
            line_width_min_pixels=1,
            stroked=True,
            get_radius=40,
            radius_min_pixels=BUS_DOT_PX,  # min = max: the same size in pixels at every zoom
            radius_max_pixels=BUS_DOT_PX,
            pickable=True,
        ),
    ]
    if highlighted:  # halo on the selected bus
        layers.insert(
            3,
            pdk.Layer(
                "ScatterplotLayer",
                data=focus,
                get_position="[lon, lat]",
                get_fill_color=[0, 0, 0, 0],
                get_line_color=[30, 30, 30],
                line_width_min_pixels=2,
                stroked=True,
                filled=False,
                get_radius=90,
                radius_min_pixels=HALO_PX,
                radius_max_pixels=HALO_PX,
                pickable=False,
            ),
        )
    tooltip = {
        "html": "<b>Route {route} → {headsign}</b><br/>"
        "<span style='color:#bbb'>destination shown on the bus</span><br/>{stop_line}"
        "Next stop: {next_stop}, predicted {next_at}<br/>"
        "Stop after: {then}, predicted {then_at}<br/>"
        "{running}<br/>{speed}{load}<br/>"
        "<span style='color:#999'>position reported {reported}</span>",
        "style": {"backgroundColor": "#222", "color": "white", "fontSize": "13px"},
    }
    if sel_bus is not None:
        # frame the bus and its next two stops: centre on them, zoom until they fit
        pts = [(float(sel_bus["lat"]), float(sel_bus["lon"]))] + [
            (float(sel_bus[a]), float(sel_bus[b]))
            for a, b in (("next_lat", "next_lon"), ("lat2s", "lon2s"))
            if a in sel_bus and not pd.isna(sel_bus[a]) and not pd.isna(sel_bus[b])
        ]
        lats, lons = [p_[0] for p_ in pts], [p_[1] for p_ in pts]
        span = max(max(lats) - min(lats), (max(lons) - min(lons)) * 0.72, 0.002)
        # web-map zoom z shows 360 * pixels / (256 * 2**z) degrees; fit the span into a third
        # of the map's 560 px height so the surrounding streets stay in view (longitude is
        # scaled by cos(44°) ≈ 0.72 at Eugene), and never closer than neighbourhood level
        zoom = max(12.5, min(14.5, math.log2(360 * map_h * 0.33 / (256 * span))))
        view = pdk.ViewState(
            latitude=(max(lats) + min(lats)) / 2,
            longitude=(max(lons) + min(lons)) / 2,
            zoom=zoom,
            transition_duration=1200,
        )
    elif sel_route:
        view = pdk.ViewState(
            latitude=float(shown["lat"].mean()), longitude=float(shown["lon"].mean()), zoom=12
        )
    else:
        view = pdk.ViewState(latitude=44.06, longitude=-123.09, zoom=11.2)
    deck = pdk.Deck(layers=layers, initial_view_state=view, map_style=None, tooltip=tooltip)

    counts = shown["status"].value_counts()
    st.caption(
        f"{len(shown)} buses · "
        + " · ".join(f"{STATUS_LABEL.get(k, 'Unknown')}: {v}" for k, v in counts.items())
        + f" · newest report {fmt_ago(live['position_timestamp'].max(), now=data_now())} · updates as LTD's data arrives"
    )
    st.pydeck_chart(
        deck, height=map_h, on_select="rerun", selection_mode="single-object", key="live_map"
    )
    st.caption(
        "Hover a bus or a stop for details. Click a bus to choose its route and that bus: the map shows the route, "
        "zooms to the bus and draws its next stop and the one after. 'All routes' goes back to every bus. "
        "The arrow shows which way the bus is heading. With a route chosen, its path is drawn and every bus on it gets its next-stop lines. "
        "Green on time · amber early · red late · dark red 10+ min late · grey unknown."
    )
    with st.expander("Table"):
        table(
            pd.DataFrame(
                {
                    "Route": shown["route"],
                    "Toward": shown["headsign"],
                    "Next stop": shown["next_stop"],
                    "At": local_times(shown["next_time"]),
                    "Then": shown["then"],
                    "At ": local_times(shown["time2"]),
                    "Min late": late_minutes(shown["delay_s"]),
                    "Speed": shown["speed_mps"] * 2.237,
                    "Load": shown["load"],
                    "Reported": local_times(shown["position_timestamp"]),
                }
            ).sort_values("Route", key=lambda c: c.astype(str).map(lambda v: (len(v), v))),
            hide_index=True,
            width="stretch",
            column_config={
                "At": col_time("At"),
                "At ": col_time("At"),
                "Min late": col_late(),
                "Speed": col_minutes("Speed", fmt="%.0f mph"),
                "Reported": col_ago("Reported"),
            },
        )


live_map()


@st.fragment(run_every=f"{LIVE_CHECK_SECONDS}s")
def stop_arrivals(stop_id: str) -> None:
    live_status_line()
    coming, _ = stop_buses(stop_id)
    show_coming(coming, "No buses scheduled at this stop in the next 36 hours.")
    st.caption(
        f"The next {STOP_ROWS_PER_ROUTE} buses of each route and direction. 'In' is LTD's prediction; "
        "LTD only predicts trips that are about to run, so later buses show the timetable. 'Likely in' "
        "corrects LTD's prediction by how far off it has usually been for this route, time of day and "
        "number of minutes ahead; '80% of the time' is the range the bus actually arrived in, in past "
        "data. Empty where there isn't enough history yet."
    )


# ---- stop lookup -----------------------------------------------------------------
st.subheader("Next buses at a stop")
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
query = st.text_input("Stop name or the number on the sign", placeholder=hint)
if not query:
    st.write("Or start with a busy stop:")
    picked = busy_stop_buttons("live_busy")
    if picked:
        st.session_state["live_stop_id"] = picked
        st.session_state["live_stop_name"] = st.session_state.get("live_busy_name", picked)
if query:
    matches = search_stops(query)
    if matches.empty:
        st.warning("No stop matches that. Try part of a street name.")
    else:
        picked = stop_buttons(matches, "live_stop")
        if picked:
            st.session_state["live_stop_id"] = picked
            st.session_state["live_stop_name"] = st.session_state.get("live_stop_name", picked)

stop_id = st.session_state.get("live_stop_id")
if "stop" in st.query_params and not stop_id:
    stop_id = st.query_params["stop"]
    st.session_state["live_stop_id"] = stop_id
if stop_id:
    st.markdown(f"**{st.session_state.get('live_stop_name', stop_id)}**")
    st.session_state["stop_id"] = stop_id  # so the Stops page opens on the same stop
    st.page_link("views/stops.py", label="How reliable is this stop? →", icon="🚏")
    stop_arrivals(stop_id)


# ---- alerts ----------------------------------------------------------------------
st.subheader("Service alerts")
alerts = q(
    "select payload from rt.alert where fetch_id = (select max(fetch_id) from rt.fetch where feed = 'alerts')"
)
if alerts.empty:
    st.caption("No active alerts.")
else:
    for payload in alerts["payload"]:
        header = next(
            (
                t.get("text")
                for t in payload.get("header_text", {}).get("translation", [])
                if t.get("text")
            ),
            "Alert",
        )
        desc = next(
            (
                t.get("text")
                for t in payload.get("description_text", {}).get("translation", [])
                if t.get("text")
            ),
            "",
        )
        with st.expander(header):
            st.write(desc or "No details given.")
