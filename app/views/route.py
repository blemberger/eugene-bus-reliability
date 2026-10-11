"""One route's report card: lateness by hour, delay along the line, headways, comparison.

Reached from the Routes table or directly at /route?route=<route_id> (bookmarkable).
"""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from common import (
    ALL_ROUTES,
    AVERAGE_COLOUR,
    LINE_MAIN,
    LINES_HELP,
    MARKER_MAIN,
    NETWORK,
    NOT_ENOUGH_HOURLY,
    PREDICTION_OFF_NOTE,
    RANGE_HELP,
    REPORT_TABLE_HEIGHT,
    ROUTE_PALETTE,
    TREND_HELP,
    TYPICAL_HELP,
    back_button,
    card,
    clean_headsign,
    countdown_off_chart,
    countdown_off_on_route,
    current_fv,
    data_note,
    day_label,
    day_sql,
    fmt_date,
    fmt_delay,
    fmt_range,
    hour_label,
    hourly_by,
    hourly_lines_chart,
    is_mobile,
    lateness,
    line_choice,
    link_table,
    minutes_axis,
    on_time_line,
    page_filters,
    q,
    range_columns,
    range_fields,
    range_scale,
    recomputing_note,
    require_db,
    require_marts,
    route_rank,
    route_tiles,
    service_hour_key,
    service_today,
    show_chart,
    stop_link,
    trend_chart,
)

require_db()
back_button("← All routes", page="views/routes.py")
title_slot = st.empty()  # filled in once the route is known
require_marts()
# Drawn in this order, though the filters are read first: the direction, the route's numbers
# with every stop on it, the hour-by-hour chart, then the period and days filters that
# everything on the page follows, then the rest. Stop report cards are laid out the same way.
dir_slot, table_slot, hour_slot = st.container(), st.container(), st.container()
start, wt = page_filters()
FV = str(current_fv())  # schedule version in force today
wt_clause, wt_params = day_sql(wt)
period = f"{day_label(wt)} since {fmt_date(start)}"
rank = route_rank(start, wt)
if rank.empty:
    with table_slot:
        if lateness(start, None, "overall").empty:
            recomputing_note()
        else:
            st.info("No scored arrivals in the selected period yet.")
    st.stop()
names = q(
    f"select route_id, route_short_name, route_long_name from gtfs.routes where feed_version_id = {FV}"
)
rank = rank.merge(names[["route_id", "route_long_name"]], on="route_id", how="left")
rank_sorted = rank.sort_values(
    "route_short_name", key=lambda c: c.astype(str).map(lambda v: (len(v), v))
)
ids = rank_sorted["route_id"].astype(str).tolist()
short = dict(zip(ids, rank_sorted["route_short_name"].astype(str), strict=False))
long_of = dict(zip(ids, rank_sorted["route_long_name"].fillna("").astype(str), strict=False))

# the route comes from the page address (?route=...): every link to a route's report card
# carries it
route_id = st.query_params.get("route")
if route_id not in ids:
    route_id = ids[0]
    st.query_params["route"] = route_id
route_name = short[route_id]
long_name = long_of[route_id] if long_of[route_id] not in ("", route_name) else ""
full_name = route_name + (f" — {long_name}" if long_name else "")
st.set_page_config(page_title=f"Route {full_name}: how reliable? · Eugene Bus Watch")
title_slot.title(
    f"How reliable is route {route_name}" + (f" ({long_name})" if long_name else "") + "?"
)

# ---- direction: everything on the page follows it --------------------------------------------
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
    with dir_slot:
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

# ---- the route's numbers, and every stop on it ----------------------------------------------
numbers_sql = f"""
    select count(*) as n,
           percentile_cont(0.5) within group (order by delay_s) as median_delay,
           percentile_cont(0.1) within group (order by delay_s) as p10_delay_s,
           percentile_cont(0.9) within group (order by delay_s) as p90_delay_s
    from marts.fct_stop_events
    where route_id = %s and status is not null {dir_clause} and {{when}}
"""
me = q(
    numbers_sql.format(when=f"service_date >= %s {wt_clause}"),
    (route_id, *dir_params, start, *wt_params),
).iloc[0]
today = q(
    numbers_sql.format(when="service_date = %s"), (route_id, *dir_params, service_today())
).iloc[0]
n_today = int(today["n"] or 0)
along = q(
    f"""
    select e.stop_sequence, s.stop_name, bool_or(e.is_timepoint) as is_timepoint, e.stop_id,
           count(*) as n,
           percentile_cont(0.5) within group (order by e.delay_s) as median_delay,
           percentile_cont(0.1) within group (order by e.delay_s) as p10,
           percentile_cont(0.9) within group (order by e.delay_s) as p90
    from marts.fct_stop_events e
    join gtfs.stops s on s.stop_id = e.stop_id and s.feed_version_id = {FV}
    where e.route_id = %s and e.service_date >= %s and e.status is not null
      {day_sql(wt, "e.service_date", "e.weekday_type")[0]}
      {dir_clause.replace("direction_id", "e.direction_id")}
    group by 1, 2, 4 having count(*) >= 5 order by 1
    """,
    (route_id, start, *wt_params, *dir_params),
)
with table_slot, card():
    st.subheader(
        "Every stop on this route",
        help=f"{period[0].upper() + period[1:]}, in the direction chosen above. Click a column "
        "heading to sort.",
    )
    m1, m2, m3 = st.columns(3)
    m1.metric(
        "Typical bus on this route vs timetable",
        fmt_delay(me["median_delay"]),
        help=TYPICAL_HELP + f" {int(me['n'] or 0):,} arrivals measured.",
    )
    m2.metric("8 in 10 buses", fmt_range(me["p10_delay_s"], me["p90_delay_s"]), help=RANGE_HELP)
    m3.metric(
        "Today so far",
        fmt_delay(today["median_delay"]) if n_today >= 10 else "—",
        help="The typical bus on this route today, as of the latest update (every 15 minutes)"
        + (f", from {n_today:,} arrivals." if n_today else "; none measured yet."),
    )
    if along.empty:
        st.caption("Not enough scored arrivals yet.")
    else:
        along["p10_s"], along["p90_s"] = along["p10"].astype(float), along["p90"].astype(float)
        along["med_s"] = along["median_delay"].astype(float)
        lo, hi = range_scale(along, "p10_s", "p90_s")
        link_table(
            [
                {
                    "href": stop_link(r.stop_id, "").split("#")[0],
                    "hover": f"{r.stop_name}: {int(r.n):,} arrivals measured",
                    "seq": int(r.stop_sequence),
                    "stop": r.stop_name,
                    "typical": fmt_delay(r.med_s),
                    "typical_s": float(r.med_s),
                    **range_fields(r.p10_s, r.med_s, r.p90_s, lo, hi),
                }
                for r in along.itertuples()
            ],
            [
                {
                    "key": "seq",
                    "label": "#",
                    "width": "2.4em",
                    "num": True,
                    "first": "asc",
                    "help": "Order along the route",
                },
                {"key": "stop", "label": "Stop", "width": "minmax(9em, 1.4fr)", "bold": True},
                {
                    "key": "typical",
                    "label": "Typical bus",
                    "width": "7.6em",
                    "help": TYPICAL_HELP,
                    "sort": "typical_s",
                },
                *range_columns(lo, hi),
            ],
            max_height=REPORT_TABLE_HEIGHT,
            key="route_stops",
            default_sort="seq:asc",
        )

# stops in the order the bus reaches them, labelled for the charts' stop choices
stop_ids = [str(x) for x in along["stop_id"]] if not along.empty else []
stop_label = (
    {str(r.stop_id): f"{int(r.stop_sequence)}. {r.stop_name}" for r in along.itertuples()}
    if not along.empty
    else {}
)
stop_colors = {k: ROUTE_PALETTE[i % len(ROUTE_PALETTE)] for i, k in enumerate(stop_ids)}

# ---- by hour: every stop together, and any stop on its own -----------------------------------
with hour_slot, card():
    st.subheader(
        "How close to the timetable, hour by hour?",
        help=LINES_HELP.format(all="every stop on the route together", one="stop")
        + f" {period[0].upper() + period[1:]}, in the direction chosen above.",
    )
    shown = line_choice(
        stop_ids,
        stop_label,
        [ALL_ROUTES],
        key=f"route_hour_lines_{route_id}_{direction}",
        all_label="All stops on this route",
        many=True,
    )
    hourly = hourly_by(
        "stop_id", f"and route_id = %s {dir_clause}", (route_id, *dir_params), start, wt
    )
    fig = hourly_lines_chart(
        [
            {
                "label": f"route {route_name}" if k == ALL_ROUTES else stop_label[k],
                "df": hourly[hourly["key"] == k],
                "color": "#1f5f9e" if k == ALL_ROUTES else stop_colors[k],
            }
            for k in shown
        ],
        reference=lateness(start, wt, "hour"),
        reference_label=NETWORK,
        min_n=5,
    )
    if not shown:
        st.caption("Pick a line above.")
    elif fig is None:
        st.caption(NOT_ENOUGH_HOURLY.format(n=5))
    else:
        show_chart(fig)

# ---- along the route ------------------------------------------------------------------------
with card():
    st.subheader(
        "Where does the delay build up?",
        help="Lateness against the timetable at each stop, in the order the bus reaches them (left "
        "to right). The line is the typical bus; the shaded band is where 8 in 10 buses fell. A "
        "line climbing means the bus loses time against the timetable there; falling means the "
        "timetable has slack there. Hover a point for its stop and numbers.",
    )
    if along.empty:
        st.caption("Not enough scored arrivals yet.")
    else:
        along["Stop"] = along["stop_sequence"].astype(str) + ". " + along["stop_name"]
        if is_mobile():  # long stop names would leave little room for the plot on a phone
            along["Stop"] = [
                t if len(t) <= 20 else t[:19].rstrip() + "…" for t in along["Stop"].astype(str)
            ]
        for c in ("median_delay", "p10", "p90"):
            along[c] = along[c].astype(float) / 60
        stops_order = along["Stop"].tolist()
        fig = go.Figure()
        on_time_line(fig)
        fig.add_trace(
            go.Scatter(
                x=stops_order + stops_order[::-1],
                y=list(along["p90"]) + list(along["p10"])[::-1],
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
                x=stops_order,
                y=along["median_delay"],
                mode="lines+markers",
                name="Typical bus",
                line={"color": "#1f5f9e", "width": LINE_MAIN},
                marker={"size": MARKER_MAIN},
                hovertext=[
                    f"{name}<br>typical bus {fmt_delay(m)}<br>8 in 10 buses: "
                    f"{fmt_range(lo_, hi_)}<br>{int(n):,} arrivals"
                    for name, m, lo_, hi_, n in zip(
                        along["Stop"],
                        along["med_s"],
                        along["p10_s"],
                        along["p90_s"],
                        along["n"],
                        strict=False,
                    )
                ],
                hoverinfo="text",
            )
        )
        fig.update_layout(
            # a faint line up from each labelled stop, and a line to the axis under the point
            # hovered, so each point can be matched with its stop by eye
            xaxis={
                "type": "category",
                "categoryorder": "array",
                "categoryarray": stops_order,
                "tickangle": -45,
                "title": "",
                "showgrid": True,
                "gridcolor": "#e9ecee",
                "griddash": "dot",
                "showspikes": True,
                "spikemode": "across",
                "spikedash": "dot",
                "spikecolor": "#9aa0a6",
                "spikethickness": 1,
            },
            yaxis=minutes_axis(list(along["p10"]) + list(along["p90"])),
            height=460 if is_mobile() else 520,
            legend={"orientation": "h", "yanchor": "bottom", "y": 1.02, "x": 0, "xanchor": "left"},
            margin={"l": 10, "r": 10, "t": 30, "b": 10},
            hovermode="closest",
        )
        show_chart(fig)

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
    with card():
        head = head.assign(_k=head["hour_local"].map(service_hour_key)).sort_values("_k")
        st.subheader(
            "Frequent-service check: do the buses come evenly?",
            help="On routes scheduled every 20 minutes or better, riders don't check a timetable; "
            "what matters is the gap between buses. Bunched = arrived less than half the "
            "scheduled gap after the previous bus. Big gap = more than 1.5 times the scheduled "
            "gap. Hover a row for the number of buses.",
        )
        link_table(
            [
                {
                    "hover": f"{int(r.n):,} buses measured",
                    "hour": hour_label(int(r.hour_local)),
                    "hour_s": service_hour_key(int(r.hour_local)),
                    "sched": f"{r.sched / 60:.0f} min",
                    "sched_s": float(r.sched),
                    "obs": f"{r.obs / 60:.1f} min",
                    "obs_s": float(r.obs),
                    "bunched": f"{r.bunched / r.n:.0%}",
                    "bunched_s": float(r.bunched / r.n),
                    "gapped": f"{r.gapped / r.n:.0%}",
                    "gapped_s": float(r.gapped / r.n),
                }
                for r in head.itertuples()
            ],
            [
                {
                    "key": "hour",
                    "label": "Hour",
                    "width": "minmax(4em, 1fr)",
                    "bold": True,
                    "sort": "hour_s",
                    "first": "asc",
                },
                {
                    "key": "sched",
                    "label": "Scheduled gap",
                    "width": "minmax(5em, 1fr)",
                    "num": True,
                    "sort": "sched_s",
                },
                {
                    "key": "obs",
                    "label": "Actual gap (typical)",
                    "width": "minmax(6em, 1fr)",
                    "num": True,
                    "sort": "obs_s",
                },
                {
                    "key": "bunched",
                    "label": "Bunched",
                    "width": "minmax(4.5em, 1fr)",
                    "num": True,
                    "sort": "bunched_s",
                },
                {
                    "key": "gapped",
                    "label": "Big gap",
                    "width": "minmax(4.5em, 1fr)",
                    "num": True,
                    "sort": "gapped_s",
                },
            ],
            max_height=520,
            key="route_headways",
            default_sort="hour:asc",
        )


# ---- trend --------------------------------------------------------------------
with card():
    st.subheader(
        "Is it getting better?",
        help=TREND_HELP + " Every stop on the route, in the direction chosen above.",
    )
    trend_chart(f"and route_id = %s {dir_clause}", (route_id, *dir_params), "on this route")

# ---- how far off the predictions are on this route -------------------------------------------
with card():
    st.subheader(
        "How far off are the predictions on this route?",
        help=PREDICTION_OFF_NOTE
        + f" {period[0].upper() + period[1:]}, in the direction chosen above. More on the "
        "Predictions page.",
    )
    pred_shown = line_choice(
        stop_ids,
        stop_label,
        [ALL_ROUTES],
        key=f"route_pred_lines_{route_id}_{direction}",
        all_label="All stops on this route",
        many=True,
    )
    on_route = countdown_off_on_route(
        route_id, direction, start, wt, [k for k in pred_shown if k != ALL_ROUTES]
    )
    fig = countdown_off_chart(
        on_route,
        stop_label,
        pred_shown,
        all_label="All stops on this route",
        colors={ALL_ROUTES: AVERAGE_COLOUR, **stop_colors},
    )
    if fig is None:
        st.caption("Not enough measured arrivals with a prediction yet for this choice.")
    else:
        show_chart(fig)

# ---- the other routes --------------------------------------------------------------------
with card():
    st.subheader("Other routes")
    route_tiles(rank_sorted[rank_sorted["route_id"].astype(str) != route_id])

st.divider()
data_note(start)
