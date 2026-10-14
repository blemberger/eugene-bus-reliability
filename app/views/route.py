"""One route's report card: its stops, lateness by hour, delay along the line, headways,
trend and predictions.

Reached from the Routes table or directly at /route?route=<route_id> (bookmarkable).
"""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from common import (
    BRAND,
    LINE_MAIN,
    MARKER_MAIN,
    REPORT_TABLE_HEIGHT,
    back_button,
    card,
    clean_headsign,
    current_fv,
    data_note,
    day_label,
    day_sql,
    fmt_date,
    fmt_delay,
    fmt_range,
    headline_metrics,
    hour_label,
    is_mobile,
    lateness,
    link_table,
    minutes_axis,
    on_time_line,
    page_filters,
    predictions_card,
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
    times_of_day_card,
    trend_card,
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
    select count(*) as n, avg(abs(delay_s)) as mean_abs_delay_s,
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
    f"""
    select count(*) as n, percentile_cont(0.5) within group (order by delay_s) as median_delay_s
    from marts.fct_stop_events
    where route_id = %s and status is not null {dir_clause} and service_date = %s
    """,
    (route_id, *dir_params, service_today()),
)
along = q(
    f"""
    select e.stop_sequence, s.stop_name, bool_or(e.is_timepoint) as is_timepoint, e.stop_id,
           count(*) as n, avg(abs(e.delay_s)) as mean_abs,
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
        f"How reliable is each stop on route {route_name}?",
        help=f"{period[0].upper() + period[1:]}, in the direction chosen above, in the order the "
        "bus reaches them. Click a column heading to sort.",
    )
    headline_metrics(me, today, start, " on this route vs timetable")
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
                    **range_fields(r.p10_s, r.med_s, r.p90_s, lo, hi, r.mean_abs),
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
                {"key": "stop", "label": "Stop", "width": "minmax(10em, 2fr)", "bold": True},
                *range_columns(lo, hi),
            ],
            max_height=REPORT_TABLE_HEIGHT,
            key="route_stops",
            default_sort="seq:asc",
        )

# ---- times of day: every stop on the route, or one -----------------------------------------
ON_ROUTE = (route_id, route_name)
IN_DIRECTION = ", in the direction chosen above"
with hour_slot:
    times_of_day_card(
        "route_tod",
        start,
        wt,
        period,
        fixed_route=ON_ROUTE,
        direction=direction,
        help_extra=IN_DIRECTION,
    )

# ---- along the route ------------------------------------------------------------------------
with card():
    st.subheader(
        f"Where along route {route_name} does the delay build up?",
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
                line={"color": BRAND, "width": LINE_MAIN},
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
            f"Do route {route_name}'s buses come evenly, or bunch up?",
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


# ---- the trend and the predictions: every stop on the route, or one ------------------------
trend_card("route_trend", fixed_route=ON_ROUTE, direction=direction, help_extra=IN_DIRECTION + ".")
predictions_card(
    "route_pred",
    start,
    wt,
    period,
    fixed_route=ON_ROUTE,
    direction=direction,
    help_extra=IN_DIRECTION,
)

# ---- the other routes --------------------------------------------------------------------
with card():
    st.subheader("Other routes")
    route_tiles(rank_sorted[rank_sorted["route_id"].astype(str) != route_id])

st.divider()
data_note(start)
