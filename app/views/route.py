"""One route's report card: lateness by hour, delay along the line, headways, comparison.

Reached from the Routes table or directly at /route?route=<route_id> (bookmarkable).
"""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from common import (
    LATENESS_CHART_NOTE,
    RANGE_HELP,
    TYPICAL_HELP,
    clean_headsign,
    col_count,
    col_hour,
    col_minutes,
    col_pct,
    col_range,
    col_stop,
    current_fv,
    data_note,
    day_label,
    day_sql,
    fit_phone,
    fmt_date,
    fmt_delay,
    fmt_range,
    hour_label,
    hour_time,
    is_mobile,
    lateness,
    lateness_chart,
    page_filters,
    q,
    recomputing_note,
    require_db,
    require_marts,
    route_rank,
    service_hour_key,
    share_pct,
    stop_link,
    table,
    today_lateness,
    worst_hour_label,
)

require_db()
title_slot = st.empty()  # filled in once the route is known (below the pickers)
require_marts()
st.page_link("views/routes.py", label="← All routes")
start, wt = page_filters()
FV = str(current_fv())  # schedule version in force today
wt_clause, wt_params = day_sql(wt)
rank = route_rank(start, wt)
if rank.empty:
    if lateness(start, None, "overall").empty:
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
        [
            sn if not ln or ln == sn else f"{sn} — {ln}"
            for sn, ln in zip(
                rank_sorted["route_short_name"].astype(str),
                rank_sorted["route_long_name"].fillna("").astype(str),
                strict=False,
            )
        ],
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
today = today_lateness(route_id=route_id)
n_today = int(today["n"].iloc[0]) if len(today) else 0
with st.container(border=True):
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Typical bus vs timetable", fmt_delay(me["median_delay"]), help=TYPICAL_HELP)
    m2.metric("8 in 10 buses", fmt_range(me["p10_delay_s"], me["p90_delay_s"]), help=RANGE_HELP)
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
        f"{int(me['n']):,}",
        f"since {fmt_date(start)}",
        delta_color="off",
        delta_arrow="off",
    )

dirs = q(
    f"""
    select direction_id, string_agg(distinct trip_headsign, ' / ') as headsigns
    from gtfs.trips where route_id = %s and feed_version_id = {FV}
    group by 1 order by 1
    """,
    (route_id,),
)


def _dir_label(direction_id: int, headsigns: str) -> str:
    raw = [h for h in str(headsigns).split(" / ") if h and h != "None"]
    clean = sorted({clean_headsign(h, route_name) for h in raw} - {""})
    if clean:
        return ("Toward " + " / ".join(clean))[:70]
    if raw:  # the headsign is only the route's own name
        return " / ".join(sorted(set(raw)))[:70] + f" (direction {direction_id})"
    return f"Direction {direction_id}"


dir_labels = {
    int(r["direction_id"]): _dir_label(int(r["direction_id"]), r["headsigns"])
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
    where e.route_id = %s and e.service_date >= %s and e.status is not null
      {wt_e} {dir_clause.replace("direction_id", "e.direction_id")}
    group by 1 order by 1
    """,
    (route_id, start, *wt_params, *dir_params),
)
for c in ("median_delay_s", "p10_delay_s", "p90_delay_s"):
    hourly[c] = hourly[c].astype(float)
with st.container(border=True):
    st.subheader(f"When is route {route_name} late?")
    fig = lateness_chart(
        hourly,
        f"route {route_name}",
        reference=lateness(start, wt, "hour"),
        min_n=5,
    )
    if fig is None:
        st.caption("Not enough arrivals yet for an hour-by-hour view (needs 5 in an hour).")
    else:
        st.plotly_chart(fit_phone(fig), width="stretch")
        st.caption(
            LATENESS_CHART_NOTE + f" Every stop on route {route_name}, {day_label(wt)} since "
            f"{fmt_date(start)}, in the direction chosen above; the dashed grey line is the typical "
            "bus on all routes together."
        )

with st.container(border=True):
    st.subheader("Where does the delay build up?")
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
        if is_mobile():  # long stop names would squeeze the plot into a sliver on a phone
            along["Stop"] = [
                t if len(t) <= 20 else t[:19].rstrip() + "…" for t in along["Stop"].astype(str)
            ]
        for c in ("median_delay", "p10", "p90"):
            along[c] = along[c] / 60
        stops_order = along["Stop"].tolist()
        fig = go.Figure()
        fig.add_vline(x=0, line_dash="dot", line_color="#999")
        fig.add_trace(
            go.Scatter(
                y=stops_order + stops_order[::-1],
                x=list(along["p90"]) + list(along["p10"])[::-1],
                fill="toself",
                fillcolor="rgba(31,95,158,0.18)",
                mode="lines",
                line_width=0,
                hoverinfo="skip",
                name="8 in 10 buses",
            )
        )
        fig.add_trace(
            go.Scatter(
                y=stops_order,
                x=along["median_delay"],
                mode="lines+markers",
                name="Typical bus",
                line={"color": "#1f5f9e", "width": 2.5},
                customdata=list(
                    zip(along["n"].astype(int), along["p10"], along["p90"], strict=False)
                ),
                hovertemplate="%{y}<br>typical %{x:+.1f} min<br>8 in 10 buses: %{customdata[1]:+.0f}"
                " to %{customdata[2]:+.0f} min<br>%{customdata[0]} arrivals<extra></extra>",
            )
        )
        values = list(along["p10"]) + list(along["p90"]) + [0.0]
        fig.update_layout(
            xaxis={
                "title": "min behind timetable" if is_mobile() else "minutes behind the timetable",
                "tickformat": "+d",
                "range": [min(values) - 0.5, max(values) + 0.5],
                "zeroline": False,
                "side": "top",
            },
            yaxis={"categoryorder": "array", "categoryarray": stops_order, "autorange": "reversed"},
            height=max(320, 22 * len(stops_order) + 120),
            legend={"orientation": "h", "yanchor": "top", "y": -0.02, "x": 0, "xanchor": "left"},
            margin={"l": 10, "r": 10, "t": 40, "b": 10},
        )
        st.plotly_chart(fit_phone(fig), width="stretch")
        st.caption(
            "Lateness against the timetable at each stop, in the order the bus reaches them (top to "
            "bottom); left of the dotted line = early. The shaded band is where 8 in 10 buses fell. "
            "A line moving right means the bus loses time against the timetable there; moving left "
            "means the timetable has slack there."
        )

        worst = along.sort_values("median_delay", ascending=False).head(5)
        early = along.sort_values("p10").head(5)
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
                    "8 in 10 buses": [
                        fmt_range(a * 60, b * 60)
                        for a, b in zip(worst["p10"], worst["p90"], strict=False)
                    ],
                }
            ),
            hide_index=True,
            width="stretch",
            column_config={
                "Stop": col_stop("Stop"),
                "Typical bus": col_minutes("Typical bus", fmt="%+.1f min", help=TYPICAL_HELP),
                "8 in 10 buses": col_range("8 in 10 buses"),
            },
        )
        right.markdown("**Stops where buses run earliest**")
        right.dataframe(
            pd.DataFrame(
                {
                    "Stop": [
                        stop_link(i, n)
                        for i, n in zip(early["stop_id"], early["stop_name"], strict=False)
                    ],
                    "Typical bus": early["median_delay"].to_numpy(),
                    "8 in 10 buses": [
                        fmt_range(a * 60, b * 60)
                        for a, b in zip(early["p10"], early["p90"], strict=False)
                    ],
                }
            ),
            hide_index=True,
            width="stretch",
            column_config={
                "Stop": col_stop("Stop"),
                "Typical bus": col_minutes("Typical bus", fmt="%+.1f min", help=TYPICAL_HELP),
                "8 in 10 buses": col_range("8 in 10 buses"),
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
    with st.container(border=True):
        head = head.assign(_k=head["hour_local"].map(service_hour_key)).sort_values("_k")
        st.subheader("Frequent-service check: do the buses come evenly?")
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
with st.container(border=True):
    st.subheader("Compare two routes")
    other = st.selectbox(
        "Compare with", [r for r in ids if r != route_id], format_func=lambda r: full[r]
    )
    if other is None:
        st.caption(
            "Only one route has scored arrivals in this period, so there is nothing to compare."
        )
    else:
        cmp = rank[rank["route_id"].isin([route_id, other])].set_index("route_short_name")
        table(
            pd.DataFrame(
                {
                    r: {
                        "Typical bus vs timetable": fmt_delay(cmp.loc[r, "median_delay"]),
                        "8 in 10 buses": fmt_range(
                            cmp.loc[r, "p10_delay_s"], cmp.loc[r, "p90_delay_s"]
                        ),
                        "Latest hour": worst_hour_label(
                            cmp.loc[r, "worst_hour"], cmp.loc[r, "worst_delay_s"]
                        ),
                        "Arrivals": f"{int(cmp.loc[r, 'n']):,}",
                    }
                    for r in cmp.index
                }
            ),
            width="content",
        )
        st.caption(f"{day_label(wt).capitalize()} since {fmt_date(start)}, every stop.")

st.divider()
data_note(start)
