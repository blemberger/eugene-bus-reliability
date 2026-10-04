"""One route's report card: lateness by hour, delay along the line, headways, comparison.

Reached from the Routes table or directly at /route?route=<route_id> (bookmarkable).
"""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from common import (
    EARLY_HELP,
    LATE_HELP,
    LATENESS_CHART_NOTE,
    TYPICAL_HELP,
    col_count,
    col_hour,
    col_late,
    col_minutes,
    col_pct,
    col_stop,
    current_fv,
    data_note,
    day_label,
    day_sql,
    fit_phone,
    fmt_delay,
    fmt_pct,
    hour_label,
    hour_time,
    lateness,
    lateness_chart,
    page_filters,
    q,
    recomputing_note,
    require_db,
    require_marts,
    route_rank,
    share_pct,
    stop_link,
    table,
)

require_db()
title_slot = st.empty()  # filled in once the route is known (below the pickers)
require_marts()
st.page_link("views/routes.py", label="All routes", icon="⬅️")
start, wt = page_filters()
FV = str(current_fv())  # schedule version in force today
wt_clause, wt_params = day_sql(wt)
rank = route_rank(start, wt)
if rank.empty:
    if lateness("timepoints", start, None, "overall").empty:
        recomputing_note()
    else:
        st.info("No scored arrivals in the selected period yet.")
    st.stop()
names = q(
    f"select route_id, route_short_name, route_long_name from gtfs.routes where feed_version_id = {FV}"
)
rank = rank.merge(names[["route_id", "route_long_name"]], on="route_id", how="left")

# ---- one route --------------------------------------------------------------------
rank_sorted = rank.sort_values(
    "route_short_name", key=lambda c: c.astype(str).map(lambda v: (len(v), v))
)
ids = rank_sorted["route_id"].astype(str).tolist()
short = dict(zip(ids, rank_sorted["route_short_name"].astype(str), strict=False))
full = dict(
    zip(
        ids,
        rank_sorted["route_short_name"].astype(str)
        + " — "
        + rank_sorted["route_long_name"].fillna("").astype(str),
        strict=False,
    )
)
if st.session_state.get("route_id") not in ids:
    wanted = st.query_params.get("route")
    st.session_state["route_id"] = wanted if wanted in ids else ids[0]
for k in ("route_chips", "route_select"):
    if st.session_state.get(k) not in ids:
        st.session_state[k] = st.session_state["route_id"]


def _sync(source: str) -> None:
    rid = st.session_state[source]
    st.session_state["route_id"] = rid
    st.session_state["route_chips"] = rid
    st.session_state["route_select"] = rid


st.pills(
    "Route",
    ids,
    format_func=lambda r: short[r],
    key="route_chips",
    required=True,
    on_change=_sync,
    args=("route_chips",),
)
st.selectbox(
    "Or find it by name",
    ids,
    format_func=lambda r: full[r],
    key="route_select",
    on_change=_sync,
    args=("route_select",),
)
route_id = st.session_state["route_id"]
st.query_params["route"] = route_id
route_name = short[route_id]
title_slot.title(f"Route {full[route_id]}")
st.caption(
    f"Report card for {day_label(wt)}, from the date filter above. Share this page's address to link to it."
)
me = rank[rank["route_id"].astype(str) == route_id].iloc[0]
m1, m2, m3, m4 = st.columns(4)
m1.metric("Typical bus", fmt_delay(me["median_delay"]), help=TYPICAL_HELP)
m2.metric("Early (1+ min)", fmt_pct(int(me["early"]), int(me["n"])), help=EARLY_HELP)
m3.metric("5+ min late", fmt_pct(int(me["late"]), int(me["n"])), help=LATE_HELP)
m4.metric("Timepoint arrivals", f"{int(me['n']):,}")

dirs = q(
    f"""
    select direction_id, string_agg(distinct trip_headsign, ' / ') as headsigns
    from gtfs.trips where route_id = %s and feed_version_id = {FV}
    group by 1 order by 1
    """,
    (route_id,),
)
dir_labels = {
    int(r["direction_id"]): f"Toward {r['headsigns'][:60]}"
    for _, r in dirs.iterrows()
    if pd.notna(r["direction_id"])
}
direction = None
if dir_labels:
    direction = st.segmented_control(
        "Direction",
        list(dir_labels.keys()),
        format_func=lambda d: dir_labels[d],
        default=next(iter(dir_labels)),
        key=f"route_dir_{route_id}",
    )
    if direction is None:  # clicking the selected option clears it; fall back to the first
        direction = next(iter(dir_labels))
dir_clause = "and direction_id = %s" if direction is not None else ""
dir_params: tuple = (direction,) if direction is not None else ()

# ---- by hour: this route against all routes ------------------------------------------------
wt_e = day_sql(wt, "e.service_date", "e.weekday_type")[0]
hourly = q(
    f"""
    select e.hour_local, count(*) as n,
           count(*) filter (where e.status = 'early') as n_early,
           count(*) filter (where e.status = 'late') as n_late,
           percentile_cont(0.5) within group (order by e.delay_s) as median_delay_s,
           percentile_cont(0.1) within group (order by e.delay_s) as p10_delay_s,
           percentile_cont(0.9) within group (order by e.delay_s) as p90_delay_s,
           count(distinct e.service_date) as n_days
    from marts.fct_stop_events e
    where e.route_id = %s and e.service_date >= %s and e.status is not null and e.is_timepoint
      {wt_e} {dir_clause.replace("direction_id", "e.direction_id")}
    group by 1 order by 1
    """,
    (route_id, start, *wt_params, *dir_params),
)
for c in ("median_delay_s", "p10_delay_s", "p90_delay_s"):
    hourly[c] = hourly[c].astype(float)
st.markdown(f"**When is route {route_name} late?**")
fig = lateness_chart(
    hourly,
    f"route {route_name}",
    reference=lateness("timepoints", start, wt, "hour"),
    min_n=5,
)
if fig is None:
    st.caption("Not enough arrivals yet for an hour-by-hour view (needs 5 in an hour).")
else:
    st.plotly_chart(fit_phone(fig), width="stretch")
    st.caption(
        LATENESS_CHART_NOTE + f" Timepoint arrivals, {day_label(wt)}, in the direction chosen "
        "above; the dashed grey line is the typical bus on all routes together."
    )

# delay along the route
st.markdown("**Where does the delay build up?**")
along = q(
    f"""
    select e.stop_sequence, s.stop_name, e.is_timepoint, e.stop_id,
           count(*) as n,
           percentile_cont(0.5) within group (order by e.delay_s) as median_delay,
           percentile_cont(0.1) within group (order by e.delay_s) as p10,
           percentile_cont(0.9) within group (order by e.delay_s) as p90,
           count(*) filter (where e.status = 'early') as early, count(*) filter (where e.status = 'late') as late
    from marts.fct_stop_events e
    join gtfs.stops s on s.stop_id = e.stop_id and s.feed_version_id = {FV}
    where e.route_id = %s and e.service_date >= %s and e.status is not null {day_sql(wt, "e.service_date", "e.weekday_type")[0]} {dir_clause.replace("direction_id", "e.direction_id")}
    group by 1, 2, 3, 4 having count(*) >= 5 order by 1
    """,
    (route_id, start, *wt_params, *dir_params),
)
if along.empty:
    st.caption("Not enough scored arrivals yet.")
else:
    along["Stop"] = along["stop_sequence"].astype(str) + ". " + along["stop_name"]
    for c in ("median_delay", "p10", "p90"):
        along[c] = along[c] / 60
    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=along["Stop"],
            y=along["p90"],
            mode="lines",
            line={"width": 0},
            showlegend=False,
            hoverinfo="skip",
        )
    )
    fig.add_trace(
        go.Scatter(
            x=along["Stop"],
            y=along["p10"],
            mode="lines",
            line={"width": 0},
            fill="tonexty",
            fillcolor="rgba(31,119,180,0.15)",
            name="10th–90th percentile",
        )
    )
    fig.add_trace(
        go.Scatter(
            x=along["Stop"],
            y=along["median_delay"],
            mode="lines+markers",
            name="Typical (median)",
            line={"color": "#1f77b4"},
            customdata=along["n"].astype(int),
            hovertemplate="%{x}<br>typical %{y:+.1f} min · %{customdata} arrivals<extra></extra>",
        )
    )
    fig.add_hline(y=0, line_dash="dot", line_color="grey")
    fig.add_hrect(y0=-1, y1=5, fillcolor="rgba(46,139,87,0.08)", line_width=0)
    fig.update_layout(
        yaxis_title="minutes late (negative = early)",
        xaxis_title="",
        xaxis_tickangle=-45,
        height=450,
        legend_title="",
    )
    st.plotly_chart(fit_phone(fig), width="stretch")
    st.caption(
        "Lateness at each stop along the trip. The green band is the on-time window. A rising line means the schedule loses time along the route; a drop means the timetable has slack there."
    )

    worst = along.sort_values("median_delay", ascending=False).head(5)
    early = (
        along.assign(early_share=along["early"] / along["n"])
        .sort_values("early_share", ascending=False)
        .head(5)
    )
    left, right = st.columns(2)
    left.markdown("**Latest stops**")
    left.dataframe(
        pd.DataFrame(
            {
                "Stop": [
                    stop_link(i, n)
                    for i, n in zip(worst["stop_id"], worst["stop_name"], strict=False)
                ],
                "Typical bus": worst["median_delay"],
                "Arrivals": worst["n"].astype(int),
            }
        ),
        hide_index=True,
        width="stretch",
        column_config={
            "Stop": col_stop("Stop"),
            "Typical bus": col_late("Typical bus (min late)"),
            "Arrivals": col_count("Arrivals"),
        },
    )
    right.markdown("**Stops where buses run early**")
    right.dataframe(
        pd.DataFrame(
            {
                "Stop": [
                    stop_link(i, n)
                    for i, n in zip(early["stop_id"], early["stop_name"], strict=False)
                ],
                "Early": share_pct(early["early"], early["n"]).to_numpy(),
                "Arrivals": early["n"].astype(int).to_numpy(),
            }
        ),
        hide_index=True,
        width="stretch",
        column_config={
            "Stop": col_stop("Stop"),
            "Early": col_pct("Early", bar=True),
            "Arrivals": col_count("Arrivals"),
        },
    )

# headways
head = q(
    f"""
    select extract(hour from scheduled_arrival at time zone 'America/Los_Angeles')::int as hour_local,
           count(*) as n,
           percentile_cont(0.5) within group (order by scheduled_headway_s) as sched,
           percentile_cont(0.5) within group (order by observed_headway_s) as obs,
           count(*) filter (where observed_headway_s < 0.5 * scheduled_headway_s) as bunched,
           count(*) filter (where observed_headway_s > 1.5 * scheduled_headway_s) as gapped
    from (
        select *, case extract(isodow from service_date)
                      when 6 then 'saturday' when 7 then 'sunday' else 'weekday' end as weekday_type
        from intermediate.int_headways
    ) h
    where route_id = %s and service_date >= %s
      and scheduled_headway_s > 0 and scheduled_headway_s <= 1200 {wt_clause} {dir_clause}
    group by 1 order by 1
    """,
    (route_id, start, *wt_params, *dir_params),
)
if not head.empty:
    st.markdown("**Frequent-service check: do the buses come evenly?**")
    st.caption(
        "On routes scheduled every 20 minutes or better, riders don't check a timetable; what matters is the gap between buses."
    )
    head["Hour"] = head["hour_local"].map(hour_label)
    tbl = pd.DataFrame(
        {
            "Hour": [hour_time(h) for h in head["hour_local"]],
            "Scheduled gap": (head["sched"] / 60).to_numpy(),
            "Actual gap (typical)": (head["obs"] / 60).to_numpy(),
            "Bunched": share_pct(head["bunched"], head["n"]).to_numpy(),
            "Big gap": share_pct(head["gapped"], head["n"]).to_numpy(),
            "Buses": head["n"].astype(int).to_numpy(),
        }
    )
    table(
        tbl,
        hide_index=True,
        width="stretch",
        column_config={
            "Hour": col_hour("Hour"),
            "Scheduled gap": col_minutes("Scheduled gap", fmt="%.1f min"),
            "Actual gap (typical)": col_minutes("Actual gap (typical)", fmt="%.1f min"),
            "Bunched": col_pct("Bunched"),
            "Big gap": col_pct("Big gap"),
            "Buses": col_count("Buses"),
        },
    )
    st.caption(
        "Bunched = arrived less than half the scheduled gap after the previous bus. Big gap = more than 1.5× the scheduled gap."
    )

# ---- compare two routes -------------------------------------------------------------
st.subheader("Compare two routes")
other = st.selectbox(
    "Compare with", [r for r in ids if r != route_id], format_func=lambda r: full[r]
)
if other is None:
    st.caption("Only one route has scored arrivals in this period, so there is nothing to compare.")
    st.divider()
    data_note(start)
    st.stop()
other_id = other
cmp = rank[rank["route_id"].isin([route_id, other_id])].set_index("route_short_name")
cmp_tbl = pd.DataFrame(
    {
        r: {
            "Typical bus": fmt_delay(cmp.loc[r, "median_delay"]),
            "Early (1+ min)": fmt_pct(int(cmp.loc[r, "early"]), int(cmp.loc[r, "n"])),
            "5+ min late": fmt_pct(int(cmp.loc[r, "late"]), int(cmp.loc[r, "n"])),
            "Worst hour": hour_label(cmp.loc[r, "worst_hour"])
            if pd.notna(cmp.loc[r, "worst_hour"])
            else "—",
            "Arrivals": int(cmp.loc[r, "n"]),
        }
        for r in cmp.index
    }
)
table(cmp_tbl, width="content")

st.divider()
data_note(start)
