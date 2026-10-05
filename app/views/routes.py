"""Routes: how late the buses run by hour, all routes together; every route in one table (click a
row for its report card)."""

from __future__ import annotations

import streamlit as st
from common import (
    LATENESS_CHART_NOTE,
    RANGE_HELP,
    TYPICAL_HELP,
    data_note,
    day_label,
    fit_phone,
    fmt_date,
    fmt_delay,
    lateness,
    lateness_chart,
    link_table,
    marts_ready,
    page_filters,
    range_cell,
    range_scale,
    recomputing_note,
    require_db,
    require_marts,
    route_link,
    route_rank,
    routes_by_service,
    today_lateness,
    worst_hour_label,
)

require_db()
st.title("Route report cards")
require_marts()
start, wt = page_filters()
rank = route_rank(start, wt)
if rank.empty:
    if marts_ready() and lateness(start, None, "overall").empty:
        recomputing_note()
    else:
        st.info("No scored arrivals in the selected period yet.")
    st.stop()

routes = routes_by_service()
long_names = dict(
    zip(routes["route_id"].astype(str), routes["route_long_name"].fillna(""), strict=False)
)
rank = rank.assign(rkey=rank["route_short_name"].astype(str).map(lambda v: (len(v), v)))
rank = rank.sort_values("rkey").reset_index(drop=True)

# ---- when are buses late: all routes together ---------------------------------------------
with st.container(border=True):
    st.subheader(f"When are buses late? All routes, {day_label(wt)}")
    fig = lateness_chart(lateness(start, wt, "hour"), "all routes")
    if fig is None:
        st.caption("Not enough arrivals yet for an hour-by-hour view.")
    else:
        st.plotly_chart(fit_phone(fig), width="stretch")
        st.caption(
            LATENESS_CHART_NOTE + f" Every stop, {day_label(wt)} since {fmt_date(start)}. "
            "Open a route below to see it on its own."
        )


# ---- every route ----------------------------------------------------------------------
def _open_route() -> None:
    rid = st.session_state.get("routes_open")
    if rid:
        # the report card page keeps its choice in these keys; set them all to this route
        for k in ("route_id", "route_chips", "route_select"):
            st.session_state[k] = rid
        st.session_state["routes_go"] = rid
    st.session_state["routes_open"] = None  # so coming back here doesn't jump away again


with st.container(border=True):
    st.subheader("Every route")
    labels = dict(
        zip(rank["route_id"].astype(str), rank["route_short_name"].astype(str), strict=False)
    )
    st.pills(
        "Open a route's report card",
        list(labels),
        format_func=lambda r: labels[r],
        key="routes_open",
        on_change=_open_route,
    )
    go_to = st.session_state.pop("routes_go", None)
    if go_to:
        st.switch_page("views/route.py", query_params={"route": go_to})

    today_r = today_lateness(group_by_route=True)
    today_of = {
        str(r.route_id): (r.median_delay_s if r.n >= 10 else None) for r in today_r.itertuples()
    }
    order = st.segmented_control(
        "Sort by",
        ["Route number", "Latest first", "Least predictable first"],
        default="Route number",
        key="routes_sort",
    )
    if order == "Latest first":
        rank = rank.sort_values("median_delay", ascending=False)
    elif order == "Least predictable first":
        rank = rank.assign(w=rank["p90_delay_s"] - rank["p10_delay_s"]).sort_values(
            "w", ascending=False
        )
    lo, hi = range_scale(rank)
    link_table(
        [
            {
                "href": route_link(r.route_id, r.route_short_name).split("#")[0],
                "route": r.route_short_name,
                "name": long_names.get(str(r.route_id), ""),
                "typical": fmt_delay(r.median_delay),
                "range": range_cell(r.p10_delay_s, r.median_delay, r.p90_delay_s, lo, hi),
                "worst": worst_hour_label(r.worst_hour, r.worst_delay_s),
                "today": fmt_delay(today_of.get(str(r.route_id))),
                "n": f"{int(r.n):,}",
            }
            for r in rank.itertuples()
        ],
        [
            {"key": "route", "label": "Route", "width": "3.2em", "bold": True},
            {"key": "name", "label": "Name", "width": "minmax(6em, 1.3fr)", "hide_on_phone": True},
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
                "key": "worst",
                "label": "Latest hour",
                "width": "minmax(9.5em, 0.9fr)",
                "hide_on_phone": True,
                "help": "The hour of day when the typical bus on this route runs latest, and how "
                "late it is then (hours with at least 10 arrivals).",
            },
            {
                "key": "today",
                "label": "Today so far",
                "width": "minmax(6.5em, 0.7fr)",
                "hide_on_phone": True,
            },
            {"key": "n", "label": "Arrivals", "width": "5em", "num": True, "hide_on_phone": True},
        ],
        max_height=900,
    )
    st.caption(
        f"Every scored arrival at every stop, {day_label(wt)} since {fmt_date(start)}. The bar "
        "shows where 8 in 10 buses fell, with a tick at the typical bus and a faint line at on "
        "schedule. Click anywhere on a row for that route's report card."
    )

st.divider()
data_note(start)
