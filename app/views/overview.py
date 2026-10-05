"""Overview (the home page): how reliable are Eugene's buses right now and over the last week?"""

from __future__ import annotations

from datetime import timedelta

import streamlit as st
from common import (
    EARLY_HELP,
    LATE_HELP,
    LATENESS_CHART_NOTE,
    LIVE_CHECK_SECONDS,
    TYPICAL_HELP,
    col_pct,
    col_route,
    col_typical,
    collection_start,
    empty_message,
    fit_phone,
    fmt_ago,
    fmt_date,
    fmt_delay,
    fmt_pct,
    lateness,
    lateness_chart,
    live_marker,
    live_status_line,
    local_today,
    marts_ready,
    q,
    q_fresh,
    recomputing_note,
    require_db,
    route_colors,
    route_link,
    share_pct,
)

require_db()

st.title("How reliable are Eugene's buses?")
c_a, c_b, c_c, c_d = st.columns(4)
c_a.page_link("views/map.py", label="Where's my bus?", icon="📍")
c_b.page_link("views/stops.py", label="How reliable is my stop?", icon="🚏")
c_c.page_link("views/routes.py", label="Route report cards", icon="🗺️")
c_d.page_link("views/accuracy.py", label="Can I trust the sign?", icon="⏱️")
st.caption(
    "Independent measurements from Lane Transit District's public schedule and live vehicle feeds. "
    "Not affiliated with LTD."
)

# ---- live strip ------------------------------------------------------------------
fresh = q("""
    select max(fetched_at) as last_fetch,
           (select count(distinct vehicle_id) from rt.vehicle_position
             where position_timestamp > now() - interval '5 minutes') as buses_now
    from rt.fetch where feed = 'vehicle_positions'
""")
last_fetch = fresh["last_fetch"][0]
buses_now = int(fresh["buses_now"][0] or 0)

start = collection_start()
week_ago = local_today() - timedelta(days=7)

if marts_ready():
    kpi = lateness("timepoints", week_ago, None, "overall")
    cal = q("""
        select n_predictions, n_within_1min from marts.mart_calibration
        where route_id is null and horizon_min = 5
    """)
    n = int(kpi["n"].iloc[0]) if len(kpi) else 0
    c1, c2, c3b, c3, c4 = st.columns(5)
    c1.metric(
        "Typical bus vs timetable, last 7 days",
        fmt_delay(kpi["median_delay_s"].iloc[0]) if n else "—",
        help=TYPICAL_HELP + " Timepoints, last 7 days.",
    )
    c2.metric(
        "Early vs timetable (1+ min)",
        fmt_pct(int(kpi["n_early"].iloc[0]) if n else 0, n),
        help=EARLY_HELP,
    )
    c3b.metric(
        "5+ min late vs timetable",
        fmt_pct(int(kpi["n_late"].iloc[0]) if n else 0, n),
        help=LATE_HELP,
    )
    c3.metric(
        "Sign accurate 5 min out",
        fmt_pct(int(cal["n_within_1min"][0]), int(cal["n_predictions"][0])) if len(cal) else "—",
        help="Share of 'arriving in 5 minutes' predictions that were right to within one minute.",
    )
    c4.metric("Buses reporting now", buses_now, fmt_ago(last_fetch), delta_color="off")
    if n == 0 and kpi.empty and marts_ready():
        recomputing_note()
    elif n == 0:
        st.info(
            "No arrivals have been scored yet. They appear once the analysis has run on a few hours of collected bus positions; it refreshes every 15 minutes."
        )
else:
    c1, c2 = st.columns(2)
    c1.metric("Buses reporting now", buses_now, fmt_ago(last_fetch), delta_color="off")
    c2.metric("Collecting since", fmt_date(start) if start else "—")
    st.info(
        "Reliability numbers appear once the analysis has run on a few hours of collected bus positions. Live data is on the Live map page."
    )

# ---- lateness by hour ------------------------------------------------------------
if marts_ready():
    st.subheader("When are buses least reliable?")
    fig = lateness_chart(lateness("timepoints", week_ago, None, "hour"), "all routes")
    if fig is not None:
        st.plotly_chart(fit_phone(fig), width="stretch")
        st.caption(
            LATENESS_CHART_NOTE + " Timepoint arrivals, all routes, last 7 days. "
            "By route and by stop, with a choice of days: the Routes and Stops pages."
        )

    # ---- best / worst routes ----------------------------------------------------
    routes = lateness("timepoints", week_ago, None, "route")
    if not routes.empty:
        routes = routes[routes["n"] >= 50].rename(
            columns={"route_short_name": "route", "n_early": "early", "n_late": "late"}
        )
        routes = routes.assign(off=(routes["early"] + routes["late"]) / routes["n"]).sort_values(
            "off"
        )
    if not routes.empty:
        routes["Typical bus"] = routes["median_delay_s"] / 60
        routes["Early"] = share_pct(routes["early"], routes["n"]).to_numpy()
        routes["Late"] = share_pct(routes["late"], routes["n"]).to_numpy()
        routes["Route"] = [
            route_link(r, s) for r, s in zip(routes["route_id"], routes["route"], strict=False)
        ]
        show = routes[["Route", "Typical bus", "Early", "Late"]]
        cfg = {
            "Route": col_route("Route"),
            "Typical bus": col_typical(max_minutes=max(5.0, float(routes["Typical bus"].max()))),
            "Early": col_pct("Early vs timetable (1+ min)", help=EARLY_HELP),
            "Late": col_pct("5+ min late vs timetable", help=LATE_HELP),
        }
        left, right = st.columns(2)
        left.subheader("Most reliable routes")
        left.dataframe(show.head(5), hide_index=True, width="stretch", column_config=cfg)
        right.subheader("Least reliable routes")
        right.dataframe(
            show.tail(5).iloc[::-1], hide_index=True, width="stretch", column_config=cfg
        )
        st.caption(
            "Ranked by how often buses were more than 1 min early or 5 min late; last 7 days, "
            "routes with at least 50 timepoint arrivals."
        )

# ---- mini live map ----------------------------------------------------------------
st.page_link("views/routes.py", label="All route report cards →", icon="🗺️")

st.subheader("Buses right now")


@st.fragment(run_every=f"{LIVE_CHECK_SECONDS}s")
def buses_now() -> None:
    live_status_line()
    live = q_fresh(
        """
        select distinct on (vehicle_id) vehicle_id, route_id, latitude as lat, longitude as lon
        from rt.vehicle_position
        where position_timestamp between now() - interval '10 minutes' and now() + interval '5 minutes'
        order by vehicle_id, position_timestamp desc
        """,
        marker=live_marker()["fid"],
    )
    if live.empty:
        st.info(empty_message("No buses reporting in the last 10 minutes."))
        return
    colors = route_colors()
    live["color"] = live["route_id"].map(lambda r: colors.get(r, "#444444"))
    st.map(live, latitude="lat", longitude="lon", color="color", size=50, zoom=11, height=350)
    st.caption(
        f"{len(live)} buses, colored by route. Details and stop lookup on the Live map page."
    )


buses_now()

st.divider()
st.caption(
    f"Data collected since {fmt_date(start) if start else '—'} · last update {fmt_ago(last_fetch)} · "
    "definitions and data quality on the Data & methods page."
)
