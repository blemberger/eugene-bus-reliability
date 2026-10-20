"""Overview (the home page): how late Eugene's buses run, by hour, for any route and stop; then a
look at each other page: every route, every stop, the trend, the predictions, the live map."""

from __future__ import annotations

import streamlit as st
from common import (
    BRAND,
    LATENESS_CHART_NOTE,
    card,
    data_note,
    day_label,
    days_suffix,
    every_stop_card,
    filters_again,
    fmt_date,
    headline_metrics,
    hourly_lines_chart,
    lateness,
    lateness_title,
    marts_ready,
    not_enough,
    page_filters,
    predictions_card,
    q,
    recomputing_note,
    require_db,
    require_marts,
    route_rank,
    route_table,
    scope_picker,
    scoped_lateness,
    show_chart,
    stops_with_numbers,
    today_lateness,
    trend_card,
)

require_db()
st.title("How reliable are Eugene's buses?")
require_marts()

# The first chart, then the period and days buttons everything on the page follows, right under
# it (drawn in that order, though the buttons are read first), as on every page; they come again
# further down, after the predictions (filters_again).
first_slot, filters_slot = st.container(), st.container()
with filters_slot:
    start, wt = page_filters()
period = f"{day_label(wt)} since {fmt_date(start)}"

# ---- when are buses late: any route, any stop -----------------------------------------------
with first_slot, card():
    title = st.empty()
    scope = scope_picker("ov", in_address=True)
    chart_slot = st.container()
    title.subheader(
        lateness_title(scope),
        help=LATENESS_CHART_NOTE + f" {period[0].upper() + period[1:]}.",
    )
    tot, hourly = scoped_lateness(start, wt, scope)
    if tot is None:
        with chart_slot:
            if not scope.route_id and not scope.stop_id and lateness(start, None, "overall").empty:
                recomputing_note()
            else:
                st.info(f"No measured arrivals for {scope.label()}, {period}.")
    else:
        fig = hourly_lines_chart([{"label": scope.label(), "df": hourly, "color": BRAND}])
        with chart_slot:
            if fig is None:
                st.caption(not_enough(start, wt))
            else:
                show_chart(fig)
        headline_metrics(
            tot,
            today_lateness(route_id=scope.route_id, stop_id=scope.stop_id),
            start,
            " vs timetable",
        )

# ---- below: a look at each other page, each opening onto more ----------------------------
# every route and every stop (as on the Routes and Stops pages, whose rows open report cards),
# the trend, how far off the predictions are, and the buses on the road now
if marts_ready():
    per = route_rank(start, wt)
    if not per.empty:
        with card():
            st.subheader(
                f"Every route{days_suffix()}",
                help=f"{period[0].upper() + period[1:]}, every stop. Click a column heading to "
                "sort, again to reverse.",
            )
            route_table(per, start, key="ov_routes")
    by_stop = lateness(start, wt, "stop")
    if not by_stop.empty:
        every_stop_card(
            stops_with_numbers(by_stop), period[0].upper() + period[1:], "ov_stops", limit=150
        )
    predictions_card("ov_pred", start, wt, period)
    filters_again()  # the period and days buttons again, between the two lower charts
    trend_card("ov_trend")

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
