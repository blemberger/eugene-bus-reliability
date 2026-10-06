"""Routes: every route in one table (click a row for its report card), then how late the buses
run by hour, all routes together."""

from __future__ import annotations

import streamlit as st
from common import (
    LATENESS_CHART_NOTE,
    card,
    data_note,
    day_label,
    fit_phone,
    fmt_date,
    lateness,
    lateness_chart,
    marts_ready,
    page_filters,
    recomputing_note,
    require_db,
    require_marts,
    route_rank,
    route_table,
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


# ---- every route ----------------------------------------------------------------------
with card():
    st.subheader(
        "Every route",
        help=f"{day_label(wt).capitalize()} since {fmt_date(start)}, every stop (the filters "
        "above). Click a column heading to sort, again to reverse.",
    )
    route_table(rank, start, key="routes_all")
    st.caption("Click a route for its report card.")

# ---- when are buses late: all routes together ---------------------------------------------
with card():
    st.subheader(f"When are buses late? All routes, {day_label(wt)}", help=LATENESS_CHART_NOTE)
    fig = lateness_chart(lateness(start, wt, "hour"), "all routes")
    if fig is None:
        st.caption("Not enough arrivals yet for an hour-by-hour view.")
    else:
        st.plotly_chart(fit_phone(fig), width="stretch")

st.divider()
data_note(start)
