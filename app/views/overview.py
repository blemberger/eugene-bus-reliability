"""Overview (the home page): how late do Eugene's buses run, by hour, for any route and stop,
and how does every route compare?"""

from __future__ import annotations

import streamlit as st
from common import (
    LATENESS_CHART_NOTE,
    RANGE_HELP,
    TYPICAL_HELP,
    current_fv,
    data_note,
    day_label,
    fit_phone,
    fmt_date,
    fmt_delay,
    fmt_range,
    lateness,
    lateness_chart,
    link_table,
    marts_ready,
    page_filters,
    q,
    range_cell,
    range_scale,
    recomputing_note,
    require_db,
    require_marts,
    route_link,
    routes_by_service,
    selection_lateness,
    today_lateness,
)

require_db()
st.title("How reliable are Eugene's buses?")
st.caption(
    "Independent measurements from Lane Transit District's public schedule and live bus "
    "positions: when each bus actually reached each stop, against the printed timetable. "
    "Not affiliated with LTD."
)
require_marts()
FV = str(current_fv())
TOP_ROUTES = 10  # route buttons; the rest are in a list next to them

routes = routes_by_service()
names = dict(
    zip(routes["route_id"].astype(str), routes["route_short_name"].astype(str), strict=False)
)
long_names = dict(
    zip(routes["route_id"].astype(str), routes["route_long_name"].fillna(""), strict=False)
)
top = routes["route_id"].astype(str).head(TOP_ROUTES).tolist()
rest = sorted(
    routes["route_id"].astype(str).iloc[TOP_ROUTES:].tolist(),
    key=lambda r: (len(names[r]), names[r]),
)
MORE = "More routes…"


def _pick_top() -> None:
    st.session_state["ov_route"] = st.session_state.get("ov_pill")
    st.session_state["ov_more"] = MORE


def _pick_more() -> None:
    choice = st.session_state.get("ov_more")
    if choice and choice != MORE:
        st.session_state["ov_route"] = choice
        st.session_state["ov_pill"] = None


# ---- when are buses late: any route, any stop -----------------------------------------------
with st.container(border=True):
    st.subheader("When are buses late?")
    c_routes, c_more = st.columns([4, 1], vertical_alignment="bottom")
    with c_routes:
        st.pills(
            "Route (busiest first; none picked = all routes)",
            sorted(top, key=lambda r: (len(names[r]), names[r])),
            format_func=lambda r: names[r],
            key="ov_pill",
            on_change=_pick_top,
        )
    with c_more:
        st.selectbox(
            "Other routes",
            [MORE, *rest],
            format_func=lambda r: r if r == MORE else names[r],
            key="ov_more",
            on_change=_pick_more,
        )
    route_id = st.session_state.get("ov_route")
    if route_id not in names:
        route_id = None

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
    if st.session_state.get("ov_stop") not in (ALL_STOPS, *stop_label):
        st.session_state["ov_stop"] = ALL_STOPS
    stop_choice = st.selectbox(
        "Stop (type to search)" + (f" on route {names[route_id]}" if route_id else ""),
        [ALL_STOPS, *stop_label],
        format_func=lambda s: s if s == ALL_STOPS else stop_label[s],
        key="ov_stop",
    )
    stop_id = None if stop_choice == ALL_STOPS else stop_choice
    start, wt = page_filters()

    what = (f"route {names[route_id]}" if route_id else "all routes") + (
        f" at {stop_label[stop_id].split('  ·  ')[0]}" if stop_id else ""
    )
    tot, hourly = selection_lateness(start, wt, route_id, stop_id)
    if tot is None:
        if not route_id and not stop_id and lateness(start, None, "overall").empty:
            recomputing_note()
        else:
            st.info(f"No measured arrivals for {what}, {day_label(wt)} since {fmt_date(start)}.")
    else:
        ref = lateness(start, wt, "hour") if (route_id or stop_id) else None
        fig = lateness_chart(hourly, what, reference=ref, min_n=5 if stop_id else 10)
        if fig is None:
            st.caption("Not enough arrivals yet for an hour-by-hour view (needs 5 in an hour).")
        else:
            st.plotly_chart(fit_phone(fig), width="stretch")
        today = today_lateness(route_id=route_id, stop_id=stop_id)
        n_today = int(today["n"].iloc[0]) if len(today) else 0
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Typical bus vs timetable", fmt_delay(tot["median_delay_s"]), help=TYPICAL_HELP)
        m2.metric(
            "8 in 10 buses", fmt_range(tot["p10_delay_s"], tot["p90_delay_s"]), help=RANGE_HELP
        )
        m3.metric(
            "Today so far",
            fmt_delay(today["median_delay_s"].iloc[0]) if n_today >= 10 else "—",
            f"{n_today:,} arrivals" if n_today else "none measured yet",
            delta_color="off",
            delta_arrow="off",
            help="The typical bus today, as of the latest update (every 15 minutes).",
        )
        m4.metric(
            "Arrivals measured",
            f"{int(tot['n']):,}",
            f"since {fmt_date(start)}",
            delta_color="off",
            delta_arrow="off",
        )
        st.caption(
            LATENESS_CHART_NOTE
            + f" {what[0].upper() + what[1:]}, {day_label(wt)} since {fmt_date(start)}"
            + ("; the dashed grey line is all routes together." if ref is not None else ".")
            + " Pick a route and/or a stop above to narrow it down."
        )

# ---- every route --------------------------------------------------------------------------
if marts_ready():
    per = lateness(start, wt, "route")
    if not per.empty:
        with st.container(border=True):
            st.subheader("Every route")
            today_r = today_lateness(group_by_route=True)
            today_of = {
                str(r.route_id): (r.median_delay_s if r.n >= 10 else None)
                for r in today_r.itertuples()
            }
            per = per.assign(rkey=per["route_short_name"].astype(str).map(lambda v: (len(v), v)))
            order = st.segmented_control(
                "Sort by",
                ["Route number", "Latest first", "Least predictable first"],
                default="Route number",
                key="ov_sort",
            )
            if order == "Latest first":
                per = per.sort_values("median_delay_s", ascending=False)
            elif order == "Least predictable first":
                per = per.assign(w=per["p90_delay_s"] - per["p10_delay_s"]).sort_values(
                    "w", ascending=False
                )
            else:
                per = per.sort_values("rkey")
            lo, hi = range_scale(per)
            link_table(
                [
                    {
                        "href": route_link(r.route_id, r.route_short_name).split("#")[0],
                        "route": r.route_short_name,
                        "name": long_names.get(str(r.route_id), ""),
                        "typical": fmt_delay(r.median_delay_s),
                        "range": range_cell(r.p10_delay_s, r.median_delay_s, r.p90_delay_s, lo, hi),
                        "today": fmt_delay(today_of.get(str(r.route_id))),
                        "n": f"{int(r.n):,}",
                    }
                    for r in per.itertuples()
                ],
                [
                    {"key": "route", "label": "Route", "width": "3.2em", "bold": True},
                    {
                        "key": "name",
                        "label": "Name",
                        "width": "minmax(6em, 1.2fr)",
                        "hide_on_phone": True,
                    },
                    {
                        "key": "typical",
                        "label": "Typical bus",
                        "width": "minmax(7em, 0.8fr)",
                        "help": TYPICAL_HELP,
                    },
                    {
                        "key": "range",
                        "label": "8 in 10 buses (vs timetable)",
                        "width": "minmax(13em, 1.6fr)",
                        "phone_width": "minmax(6.8em, 1fr)",
                        "html": True,
                        "help": RANGE_HELP,
                    },
                    {
                        "key": "today",
                        "label": "Today so far",
                        "width": "minmax(6.5em, 0.7fr)",
                        "hide_on_phone": True,
                    },
                    {
                        "key": "n",
                        "label": "Arrivals",
                        "width": "5em",
                        "num": True,
                        "hide_on_phone": True,
                    },
                ],
            )
            st.caption(
                f"Every scored arrival at every stop, {day_label(wt)} since {fmt_date(start)} "
                "(the filters above). The bar shows where 8 in 10 buses fell, with a tick at the "
                "typical bus and a faint line at on schedule. Click a route for its report card."
            )

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
