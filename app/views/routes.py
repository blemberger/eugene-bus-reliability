"""Routes: how late the buses run by hour, all routes together; every route's report card in one
table; pick a route to open its own page."""

from __future__ import annotations

import pandas as pd
import streamlit as st
from common import (
    EARLY_HELP,
    LATE_HELP,
    LATENESS_CHART_NOTE,
    col_count,
    col_hour,
    col_pct,
    col_route,
    col_typical,
    current_fv,
    data_note,
    day_label,
    download_button,
    fit_phone,
    hour_time,
    late_minutes,
    lateness,
    lateness_chart,
    marts_ready,
    page_filters,
    q,
    recomputing_note,
    require_db,
    require_marts,
    route_link,
    route_rank,
    share_pct,
    table,
)

require_db()
st.title("Route report cards")
require_marts()
start, wt = page_filters()
FV = str(current_fv())  # schedule version in force today
rank = route_rank(start, wt)
if rank.empty:
    if marts_ready() and lateness("timepoints", start, None, "overall").empty:
        recomputing_note()
    else:
        st.info("No scored arrivals in the selected period yet.")
    st.stop()

# ---- when are buses late: all routes together ---------------------------------------------
st.markdown(f"#### When are buses late? All routes, {day_label(wt)}")
fig = lateness_chart(lateness("timepoints", start, wt, "hour"), "all routes")
if fig is None:
    st.caption("Not enough arrivals yet for an hour-by-hour view.")
else:
    st.plotly_chart(fit_phone(fig), width="stretch")
    st.caption(
        LATENESS_CHART_NOTE + " Timepoint arrivals. Open a route below to see it on its own."
    )

names = q(
    f"select route_id, route_short_name, route_long_name from gtfs.routes where feed_version_id = {FV}"
)
rank = rank.merge(names[["route_id", "route_long_name"]], on="route_id", how="left")
rank = rank.assign(rkey=rank["route_short_name"].astype(str).map(lambda v: (len(v), v)))
rank = rank.sort_values("rkey").reset_index(drop=True)


# ---- open one route's report card -------------------------------------------------------
def _open_route() -> None:
    rid = st.session_state.get("routes_open")
    if rid:
        # the report card page keeps its choice in these keys; set them all to this route
        for k in ("route_id", "route_chips", "route_select"):
            st.session_state[k] = rid
        st.session_state["routes_go"] = rid
    st.session_state["routes_open"] = None  # so coming back here doesn't jump away again


st.markdown("**Open a route's report card**")
labels = dict(zip(rank["route_id"].astype(str), rank["route_short_name"].astype(str), strict=False))
st.pills(
    "Route",
    list(labels),
    format_func=lambda r: labels[r],
    key="routes_open",
    on_change=_open_route,
    label_visibility="collapsed",
)
go_to = st.session_state.pop("routes_go", None)
if go_to:
    st.switch_page("views/route.py", query_params={"route": go_to})

# ---- every route ----------------------------------------------------------------------
st.markdown("**Every route**")
typical = late_minutes(rank["median_delay"])
card = pd.DataFrame(
    {
        "Route": [
            route_link(r, s)
            for r, s in zip(rank["route_id"], rank["route_short_name"], strict=False)
        ],
        "Name": rank["route_long_name"].to_numpy(),
        "Typical bus": typical.to_numpy(),
        "Early": share_pct(rank["early"], rank["n"]).to_numpy(),
        "Late": share_pct(rank["late"], rank["n"]).to_numpy(),
        "Worst hour": [hour_time(h) for h in rank["worst_hour"]],
        "Arrivals": rank["n"].astype(int).to_numpy(),
    }
)
table(
    card,
    hide_index=True,
    width="stretch",
    height=min(800, 40 + 35 * len(card)),
    column_config={
        "Route": col_route("Route"),
        "Typical bus": col_typical(max_minutes=max(5.0, float(typical.max() or 0))),
        "Early": col_pct("Early vs timetable (1+ min)", help=EARLY_HELP),
        "Late": col_pct("5+ min late vs timetable", help=LATE_HELP),
        "Worst hour": col_hour(
            "Worst hour", help="The hour with the most buses early or 5+ min late."
        ),
        "Arrivals": col_count("Arrivals"),
    },
)
st.caption(
    f"Timepoint arrivals only, {day_label(wt)}. Typical bus = the median minutes behind the "
    "timetable. Click a column header to sort; click a route number to open its report card."
)
download_button(
    card.assign(Route=rank["route_short_name"].to_numpy()),
    f"eugenebuswatch_route_report_cards_{day_label(wt).replace(' ', '_')}_since_{start}.csv",
)

st.divider()
data_note(start)
