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
    COL_TOWARD_NARROW,
    LINE_MAIN,
    LIVE_CHECK_SECONDS,
    MARKER_MAIN,
    REPORT_TABLE_HEIGHT,
    back_button,
    card,
    clean_headsign,
    clean_headsigns,
    col_ago,
    col_clock,
    current_fv,
    data_note,
    day_label,
    day_sql,
    days_suffix,
    empty_message,
    filters_again,
    fmt_date,
    fmt_delay,
    fmt_range,
    headline_metrics,
    hour_label,
    is_mobile,
    late_minutes,
    lateness,
    link_table,
    live_buses,
    live_marker,
    live_status_line,
    local_times,
    minutes_axis,
    next_stops,
    on_time_line,
    page_filters,
    place_join,
    predictions_card,
    q,
    range_columns,
    range_fields,
    range_scale,
    recomputing_note,
    require_db,
    require_marts,
    route_order,
    route_rank,
    route_tiles,
    route_timetable,
    service_hour_key,
    service_today,
    show_chart,
    stop_choices,
    stop_link,
    table,
    times_of_day_card,
    timetable_card,
    trend_card,
)

require_db()
back_button("← All routes", page="views/routes.py")
title_slot = st.empty()  # filled in once the route is known
require_marts()
# Drawn in this order, though the buttons are read first: the direction (which the whole page
# follows); the chart by time of day, then right under it the period and days buttons; the
# route's numbers with every stop on it; the predictions; the buttons again; the route's own
# detail and, last, the trend (usually a flat line). Stop report cards are laid out the same way.
dir_slot, hour_slot, filters_slot = st.container(), st.container(), st.container()
table_slot, pred_slot = st.container(), st.container()
with filters_slot:
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
# each stop's place along the route in this direction, the same in the stop list, the table and
# the chart ('3. Olive & 11th'). Trips don't all make the same stops (some start or end part way,
# and a loop passes its first stop again at the end), so the order comes from fitting every
# trip's stops together (route_order), and each arrival is placed by its own trip's stop number
# (place_join): on a loop, leaving the first stop and arriving back there are two places.
if direction is not None:
    PLACE_SQL, PLACE_PARAMS = place_join(route_id, direction)
    PLACE = "coalesce(pl.place, pf.place, 9999)"
else:  # a route the timetable gives no directions: each stop once, at its earliest place
    PLACE_SQL, PLACE_PARAMS = "", ()
    PLACE = "min(e.stop_sequence) over (partition by e.stop_id)"
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
    select place, min(stop_id) as stop_id, bool_or(is_timepoint) as is_timepoint,
           count(*) as n, avg(abs(delay_s)) as mean_abs,
           percentile_cont(0.5) within group (order by delay_s) as median_delay,
           percentile_cont(0.1) within group (order by delay_s) as p10,
           percentile_cont(0.9) within group (order by delay_s) as p90
    from (
        select e.stop_id::text as stop_id, e.is_timepoint, e.delay_s, {PLACE} as place
        from marts.fct_stop_events e {PLACE_SQL}
        where e.route_id = %s and e.service_date >= %s and e.status is not null
          {day_sql(wt, "e.service_date", "e.weekday_type")[0]}
          {dir_clause.replace("direction_id", "e.direction_id")}
    ) x
    group by place having count(*) >= 5 order by place
    """,
    (*PLACE_PARAMS, route_id, start, *wt_params, *dir_params),
)
if not along.empty:
    stop_names = q(
        f"select stop_id::text as stop_id, stop_name from gtfs.stops where feed_version_id = {FV}"
    )
    along = along.merge(stop_names, on="stop_id", how="left")
    along["stop_name"] = along["stop_name"].fillna("Stop")
    if direction is not None:
        along["no"] = along["place"].astype(int) + 1
    else:  # numbered by the stop list
        _no = {str(s): i + 1 for i, s in enumerate(stop_choices(route_id, None)["stop_id"])}
        along["no"] = [_no.get(str(s), 0) for s in along["stop_id"]]
    along["Stop"] = [f"{no}. {n}" for no, n in zip(along["no"], along["stop_name"], strict=False)]
with table_slot, card():
    st.subheader(
        f"Each stop on route {route_name}{days_suffix()}",
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
                    # numbered in the order the bus reaches them, and sorted that way
                    "stop": r.Stop,
                    "stop_s": int(r.no),
                    **range_fields(r.p10_s, r.med_s, r.p90_s, lo, hi, r.mean_abs),
                }
                for r in along.itertuples()
            ],
            [
                {
                    "key": "stop",
                    "label": "Stop",
                    "width": "minmax(10em, 2fr)",
                    "bold": True,
                    "help": "In the order the bus reaches them. Sorts that way.",
                    "sort": "stop_s",
                    "first": "asc",
                },
                *range_columns(lo, hi),
            ],
            max_height=REPORT_TABLE_HEIGHT,
            key="route_stops",
            default_sort="stop:asc",
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

# ---- the period and days buttons again, for everything below -------------------------------
filters_again()

# ---- along the route ------------------------------------------------------------------------
with card():
    st.subheader(
        f"Lateness along route {route_name}{days_suffix()}",
        help="Lateness against the timetable at each stop, in the order the bus reaches them (left "
        "to right). The line is the typical bus; the shaded band is where 8 in 10 buses fell. A "
        "line climbing means the bus loses time against the timetable there; falling means the "
        "timetable has slack there. Hover a point for its stop and numbers.",
    )
    if along.empty:
        st.caption("Not enough scored arrivals yet.")
    else:
        along["label"] = along["Stop"].astype(str)
        if is_mobile():  # long stop names would leave little room for the plot on a phone
            along["label"] = [t if len(t) <= 20 else t[:19].rstrip() + "…" for t in along["label"]]
        for c in ("median_delay", "p10", "p90"):
            along[c] = along[c].astype(float) / 60
        stops_order = along["label"].tolist()
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
                legendrank=2,  # listed after the line, as on every chart
            )
        )
        fig.add_trace(
            go.Scatter(
                x=stops_order,
                y=along["median_delay"],
                mode="lines+markers",
                name="Typical bus",
                legendrank=1,
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
            f"Gaps between buses on route {route_name}{days_suffix()}",
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


# ---- the predictions (drawn under the filters) and the trend: every stop on the route, or one
with pred_slot:
    predictions_card(
        "route_pred",
        start,
        wt,
        period,
        fixed_route=ON_ROUTE,
        direction=direction,
        help_extra=IN_DIRECTION,
    )
trend_card("route_trend", fixed_route=ON_ROUTE, direction=direction, help_extra=IN_DIRECTION + ".")


# ---- right now: the buses on the route in this direction, in the order they come ------------
@st.fragment(run_every=f"{LIVE_CHECK_SECONDS}s")
def route_right_now() -> None:
    live_status_line()
    buses = live_buses(route_id, direction)
    if buses.empty:
        st.caption(
            empty_message(
                f"No route {route_name} bus in this direction has reported in the last 10 minutes."
            )
        )
    else:
        # the next stop from where the bus actually is on its route (as on the Live map), else
        # LTD's
        geo = next_stops(buses, marker=live_marker()["fid"])
        if len(geo):
            g = geo.set_index(geo["vehicle_id"].astype(str))
            vid = buses["vehicle_id"].astype(str)
            placed = vid.isin(g.index)
            for to, frm in (("next_seq", "seq1"), ("next_stop", "name1"), ("next_time", "time1")):
                buses[to] = buses[to].astype(object).where(~placed, vid.map(g[frm]))
            buses["next_id"] = vid.map(g["stop_id1"])
        else:
            buses["next_id"] = None
        place = {}
        if direction is not None:
            place = route_order(route_id, direction, current_fv())[1]
        buses["place"] = [
            place.get((str(sid), int(seq))) if pd.notna(sid) and pd.notna(seq) else None
            for sid, seq in zip(buses["next_id"], buses["next_seq"], strict=False)
        ]
        buses = buses.sort_values("place", na_position="last")
        table(
            pd.DataFrame(
                {
                    "Bus": buses["vehicle_id"].astype(str),
                    "Toward": clean_headsigns(buses),
                    "Next stop": [
                        "—" if pd.isna(n) else (f"{int(pl) + 1}. {n}" if pd.notna(pl) else str(n))
                        for n, pl in zip(buses["next_stop"], buses["place"], strict=False)
                    ],
                    "Arrives": local_times(buses["next_time"]),
                    # text, so a bus LTD gives no time for shows blank rather than "None"
                    "Min late": [
                        "" if v is None or pd.isna(v) else f"{float(v):.1f}"
                        for v in late_minutes(buses["delay_s"])
                    ],
                    "Reported": local_times(buses["position_timestamp"]),
                }
            ),
            hide_index=True,
            width="stretch",
            column_config={
                "Bus": st.column_config.TextColumn("Bus", width=60),
                "Toward": COL_TOWARD_NARROW,
                "Next stop": st.column_config.TextColumn(
                    "Next stop", help="Numbered as in the table of stops above."
                ),
                "Arrives": col_clock("Arrives", buses["next_time"]),
                "Min late": st.column_config.TextColumn(
                    "Min late",
                    help="How late LTD's prediction for its next stop puts the bus, in minutes; "
                    "negative = early.",
                ),
                "Reported": col_ago("Reported"),
            },
        )
    st.page_link(
        "views/map.py", label="See them on the live map →", query_params={"route": route_id}
    )


with card():
    st.subheader(
        f"Right now on route {route_name}",
        help="Every bus on this route, in the direction chosen above, that reported in the last 10 "
        "minutes, in the order they come along the route: its next stop, when LTD predicts it "
        "there, and how late that puts it against the timetable (negative = early).",
    )
    route_right_now()

# ---- the plain timetable, folded away: for reference --------------------------------------
if direction is not None:
    _way = dir_labels.get(direction, "")
    timetable_card(
        f"Timetable for route {route_name}" + (f", {_way[:1].lower()}{_way[1:]}" if _way else ""),
        f"tt_route_{route_id}",
        lambda day: route_timetable(route_id, direction, day),
        note="One row per bus. The columns are the route's timepoints: the main stops with an "
        "exact time in the timetable, numbered as in the table of stops above; the bus passes "
        "the stops between them in between.",
    )

# ---- the other routes --------------------------------------------------------------------
with card():
    st.subheader("Other routes")
    route_tiles(rank_sorted[rank_sorted["route_id"].astype(str) != route_id])

st.divider()
data_note(start)
