"""Routes: every route's report card in one table; click a route for its own page."""

from __future__ import annotations

import pandas as pd
import streamlit as st
from common import (
    arrow_safe,
    col_count,
    col_hour,
    col_late,
    col_pct,
    current_fv,
    data_note,
    day_label,
    download_button,
    hour_time,
    late_minutes,
    page_filters,
    q,
    require_db,
    require_marts,
    route_rank,
    share_pct,
)

require_db()
st.title("Route report cards")
require_marts()
start, wt = page_filters()
FV = str(current_fv())  # schedule version in force today
# ---- ranking ----------------------------------------------------------------------
rank = route_rank(start, wt)
if rank.empty:
    st.info("No scored arrivals in the selected period yet.")
    st.stop()

names = q(
    f"select route_id, route_short_name, route_long_name from gtfs.routes where feed_version_id = {FV}"
)
rank = rank.merge(names[["route_id", "route_long_name"]], on="route_id", how="left")
card = pd.DataFrame(
    {
        "Route": rank["route_short_name"].to_numpy(),
        "Name": rank["route_long_name"].to_numpy(),
        "On time": share_pct(rank["on_time"], rank["n"]).to_numpy(),
        "Early": share_pct(rank["early"], rank["n"]).to_numpy(),
        "Late": share_pct(rank["late"], rank["n"]).to_numpy(),
        "Typical bus": late_minutes(rank["median_delay"]).to_numpy(),
        "Worst hour": [hour_time(h) for h in rank["worst_hour"]],
        "Arrivals": rank["n"].astype(int).to_numpy(),
    }
)
table_key = f"route_table_{st.session_state.get('route_table_n', 0)}"
picked = st.dataframe(
    arrow_safe(card),
    hide_index=True,
    width="stretch",
    height=min(700, 40 + 35 * len(card)),
    on_select="rerun",
    selection_mode="single-row",
    key=table_key,
    column_config={
        "On time": col_pct("On time", bar=True),
        "Early": col_pct("Early"),
        "Late": col_pct("Late"),
        "Typical bus": col_late(
            "Typical bus (min late)", help="Median minutes late; negative = early."
        ),
        "Worst hour": col_hour("Worst hour"),
        "Arrivals": col_count("Arrivals"),
    },
)
rows = picked.selection.rows if picked is not None else []
if rows:
    st.session_state["route_table_n"] = st.session_state.get("route_table_n", 0) + 1
    st.switch_page("views/route.py", query_params={"route": str(rank.iloc[rows[0]]["route_id"])})
st.caption(
    f"Timepoint arrivals only, {day_label(wt)}. Sorted best to worst; click a column header to "
    "re-sort. **Click a route's row to open its full report card.**"
)
download_button(card, "ltd_route_report_cards.csv")

st.divider()
data_note(start)
