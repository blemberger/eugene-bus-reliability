"""Overview (the home page): how reliable are Eugene's buses right now and over the last week?"""

from __future__ import annotations

from datetime import timedelta

import plotly.express as px
import streamlit as st
from common import (
    EARLY_HELP,
    LATE_HELP,
    LIVE_CHECK_SECONDS,
    STATUS_COLOR,
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
    hour_label,
    live_marker,
    live_status_line,
    local_today,
    marts_ready,
    q,
    q_fresh,
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
    kpi = q(
        """
        select count(*) as n, count(*) filter (where status = 'on_time') as on_time,
               count(*) filter (where status = 'early') as early,
               count(*) filter (where status = 'late') as late,
               percentile_cont(0.5) within group (order by delay_s) as median_delay
        from marts.fct_stop_events
        where status is not null and is_timepoint and service_date >= %s
        """,
        (week_ago,),
    )
    cal = q("""
        select n_predictions, n_within_1min from marts.mart_calibration
        where route_id is null and horizon_min = 5
    """)
    n = int(kpi["n"][0] or 0)
    c1, c2, c3b, c3, c4 = st.columns(5)
    c1.metric(
        "Typical bus, last 7 days",
        fmt_delay(kpi["median_delay"][0]) if n else "—",
        help=TYPICAL_HELP + " Timepoints, last 7 days.",
    )
    c2.metric("Early (1+ min)", fmt_pct(int(kpi["early"][0] or 0), n), help=EARLY_HELP)
    c3b.metric("5+ min late", fmt_pct(int(kpi["late"][0] or 0), n), help=LATE_HELP)
    c3.metric(
        "Sign accurate 5 min out",
        fmt_pct(int(cal["n_within_1min"][0]), int(cal["n_predictions"][0])) if len(cal) else "—",
        help="Share of 'arriving in 5 minutes' predictions that were right to within one minute.",
    )
    c4.metric("Buses reporting now", buses_now, fmt_ago(last_fetch), delta_color="off")
    if n == 0:
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

# ---- on-time by hour ------------------------------------------------------------
if marts_ready():
    st.subheader("When are buses least reliable?")
    by_hour = q(
        """
        select hour_local, sum(n_events) as n, sum(n_on_time) as on_time, sum(n_early) as early, sum(n_late) as late
        from marts.mart_route_daily where service_date >= %s group by 1 order by 1
        """,
        (week_ago,),
    )
    if not by_hour.empty:
        by_hour["Hour"] = by_hour["hour_local"].map(hour_label)
        long = by_hour.melt(
            id_vars=["Hour", "hour_local"],
            value_vars=["early", "on_time", "late"],
            var_name="status",
            value_name="count",
        )
        long["share"] = long["count"] / long.groupby("hour_local")["count"].transform("sum")
        long["status"] = long["status"].map(
            {"early": "Early (1+ min)", "on_time": "On time", "late": "5+ min late"}
        )
        long["Arrivals"] = long["count"].astype(int)
        fig = px.bar(
            long,
            x="Hour",
            y="share",
            hover_data={"Arrivals": True, "share": ":.0%"},
            color="status",
            barmode="stack",
            color_discrete_map={
                "Early (1+ min)": STATUS_COLOR["early"],
                "On time": STATUS_COLOR["on_time"],
                "5+ min late": STATUS_COLOR["late"],
            },
            category_orders={"Hour": list(by_hour["Hour"])},
        )
        fig.update_layout(
            yaxis_tickformat=".0%",
            yaxis_title="share of timepoint arrivals",
            xaxis_title="",
            legend_title="",
        )
        st.plotly_chart(fit_phone(fig), width="stretch")
        st.caption(
            "Share of timepoint arrivals by hour of day that came more than 1 min early, on time "
            "(between those), or more than 5 min late, last 7 days."
        )

    # ---- best / worst routes ----------------------------------------------------
    routes = q(
        """
        select route_id, route_short_name as route, count(*) as n,
               count(*) filter (where status = 'early') as early,
               count(*) filter (where status = 'late') as late,
               percentile_cont(0.5) within group (order by delay_s) as median_delay
        from marts.fct_stop_events
        where status is not null and is_timepoint and service_date >= %s
        group by 1, 2 having count(*) >= 50
        order by count(*) filter (where status <> 'on_time')::float / count(*)
        """,
        (week_ago,),
    )
    if not routes.empty:
        routes["Typical bus"] = routes["median_delay"].astype(float) / 60
        routes["Early"] = share_pct(routes["early"], routes["n"]).to_numpy()
        routes["Late"] = share_pct(routes["late"], routes["n"]).to_numpy()
        routes["Route"] = [
            route_link(r, s) for r, s in zip(routes["route_id"], routes["route"], strict=False)
        ]
        show = routes[["Route", "Typical bus", "Early", "Late"]]
        cfg = {
            "Route": col_route("Route"),
            "Typical bus": col_typical(max_minutes=max(5.0, float(routes["Typical bus"].max()))),
            "Early": col_pct("Early (1+ min)", help=EARLY_HELP),
            "Late": col_pct("5+ min late", help=LATE_HELP),
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
