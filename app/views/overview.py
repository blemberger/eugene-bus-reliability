"""Overview (the home page): how late do Eugene's buses run, by hour, for any route and stop,
and how does every route compare?"""

from __future__ import annotations

import streamlit as st
from common import (
    LATENESS_CHART_NOTE,
    RANGE_HELP,
    TYPICAL_HELP,
    card,
    current_fv,
    data_note,
    day_label,
    fit_phone,
    fmt_date,
    fmt_delay,
    fmt_range,
    lateness,
    lateness_chart,
    marts_ready,
    page_filters,
    q,
    recomputing_note,
    require_db,
    require_marts,
    route_picker,
    route_rank,
    route_table,
    routes_by_service,
    selection_lateness,
    today_lateness,
)

require_db()
st.title("How reliable are Eugene's buses?", help=LATENESS_CHART_NOTE)
require_marts()
FV = str(current_fv())

routes = routes_by_service()
names = dict(
    zip(routes["route_id"].astype(str), routes["route_short_name"].astype(str), strict=False)
)

# ---- when are buses late: any route, any stop -----------------------------------------------
with card():
    route_id = route_picker(routes, key="ov_route")

    if route_id:
        stops = q(
            f"""
            select distinct s.stop_id, s.stop_name, s.stop_code
            from gtfs.stop_times st
            join gtfs.trips t on t.trip_id = st.trip_id and t.feed_version_id = st.feed_version_id
            join gtfs.stops s on s.stop_id = st.stop_id and s.feed_version_id = st.feed_version_id
            where st.feed_version_id = {FV} and t.route_id = %s
            order by s.stop_name
            """,
            (route_id,),
        )
    else:
        stops = q(f"""
            select stop_id, stop_name, stop_code from gtfs.stops
            where feed_version_id = {FV} and location_type = 0 order by stop_name
        """)
    stop_label = {
        str(r.stop_id): f"{r.stop_name}" + (f"  ·  #{r.stop_code}" if r.stop_code else "")
        for r in stops.itertuples()
    }
    ALL_STOPS = "All stops"
    # a stop that isn't on the newly chosen route goes back to all stops
    if st.session_state.get("ov_stop") not in (ALL_STOPS, *stop_label):
        st.session_state["ov_stop"] = ALL_STOPS
    stop_choice = st.selectbox(
        "Stop" + (f" on route {names[route_id]}" if route_id else "") + " (type to search)",
        [ALL_STOPS, *stop_label],
        format_func=lambda s: s if s == ALL_STOPS else stop_label[s],
        key="ov_stop",
    )
    stop_id = None if stop_choice == ALL_STOPS else stop_choice
    # the chart goes here, right under the route and stop; the period and days filters below it
    # (it is drawn after them, because it depends on them)
    chart_slot = st.container()
    start, wt = page_filters()

    what = (f"route {names[route_id]}" if route_id else "all routes") + (
        f" at {stop_label[stop_id].split('  ·  ')[0]}" if stop_id else ""
    )
    tot, hourly = selection_lateness(start, wt, route_id, stop_id)
    if tot is None:
        with chart_slot:
            if not route_id and not stop_id and lateness(start, None, "overall").empty:
                recomputing_note()
            else:
                st.info(
                    f"No measured arrivals for {what}, {day_label(wt)} since {fmt_date(start)}."
                )
    else:
        ref = lateness(start, wt, "hour") if (route_id or stop_id) else None
        fig = lateness_chart(hourly, what, reference=ref, min_n=5 if stop_id else 10)
        with chart_slot:
            if fig is None:
                st.caption("Not enough arrivals yet for an hour-by-hour view (needs 5 in an hour).")
            else:
                st.plotly_chart(fit_phone(fig), width="stretch")
        today = today_lateness(route_id=route_id, stop_id=stop_id)
        n_today = int(today["n"].iloc[0]) if len(today) else 0
        m1, m2, m3 = st.columns(3)
        m1.metric(
            "Typical bus vs timetable",
            fmt_delay(tot["median_delay_s"]),
            help=TYPICAL_HELP + f" {int(tot['n']):,} arrivals measured since {fmt_date(start)}.",
        )
        m2.metric(
            "8 in 10 buses", fmt_range(tot["p10_delay_s"], tot["p90_delay_s"]), help=RANGE_HELP
        )
        m3.metric(
            "Today so far",
            fmt_delay(today["median_delay_s"].iloc[0]) if n_today >= 10 else "—",
            help="The typical bus today, as of the latest update (every 15 minutes)"
            + (f", from {n_today:,} arrivals." if n_today else "; none measured yet."),
        )

# ---- every route --------------------------------------------------------------------------
if marts_ready():
    per = route_rank(start, wt)
    if not per.empty:
        with card():
            st.subheader(
                "Every route",
                help=f"{day_label(wt).capitalize()} since {fmt_date(start)}, every stop (the "
                "filters above). Click a column heading to sort, again to reverse.",
            )
            route_table(per, start, key="ov_routes")
            st.caption("Click a route for its report card.")
    st.page_link("views/stops.py", label="Every stop, and each stop's report card →")

n_now = q("""
    select count(distinct vehicle_id) as n from rt.vehicle_position
    where position_timestamp > now() - interval '5 minutes'
""")["n"][0]
st.page_link(
    "views/map.py",
    label=f"{int(n_now or 0)} buses on the road right now: see them on the live map →",
)
st.divider()
data_note(start)
