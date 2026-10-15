"""Routes: find your route, how reliable each route is (click a row for its report card), and
the charts for any route: times of day, the trend, the predictions."""

from __future__ import annotations

import streamlit as st
from common import (
    card,
    data_note,
    day_label,
    days_suffix,
    filters_again,
    fmt_date,
    lateness,
    marts_ready,
    page_filters,
    predictions_card,
    recomputing_note,
    require_db,
    require_marts,
    route_rank,
    route_table,
    route_tiles,
    routes_by_service,
    times_of_day_card,
    trend_card,
)

require_db()
st.title("How reliable is my route?")
require_marts()
# Find your route, then the period and days filters right above the first thing they change,
# the table and the times-of-day chart, then (with the filters again) the predictions and the
# trend, as on a route's report card
with card():
    st.subheader("Find your route")
    every = routes_by_service()
    route_tiles(
        every.assign(
            _k=every["route_short_name"].astype(str).map(lambda v: (len(v), v))
        ).sort_values("_k")
    )
start, wt = page_filters()
table_slot, chart_slot = st.container(), st.container()
period = f"{day_label(wt)} since {fmt_date(start)}"
rank = route_rank(start, wt)
if rank.empty:
    with table_slot:
        if marts_ready() and lateness(start, None, "overall").empty:
            recomputing_note()
        else:
            st.info("No scored arrivals in the selected period yet.")
    st.stop()

with table_slot, card():
    st.subheader(
        f"How reliable is each route{days_suffix()}?",
        help=f"{period[0].upper() + period[1:]}, every stop. Click a column "
        "heading to sort, again to reverse.",
    )
    route_table(rank, start, key="routes_all")

with chart_slot:
    times_of_day_card("routes_tod", start, wt, period)

filters_again()  # the period and days buttons again, for everything below
predictions_card("routes_pred", start, wt, period)
trend_card("routes_trend")

st.divider()
data_note(start)
