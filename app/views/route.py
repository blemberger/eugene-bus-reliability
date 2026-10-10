"""One route's report card: lateness by hour, delay along the line, headways, comparison.

Reached from the Routes table or directly at /route?route=<route_id> (bookmarkable).
"""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from common import (
    LATENESS_CHART_NOTE,
    LINE_MAIN,
    MARKER_MAIN,
    RANGE_HELP,
    TYPICAL_HELP,
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
    hour_label,
    is_mobile,
    lateness,
    lateness_chart,
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
    table,
    worst_hour_label,
)

require_db()
back_button("← All routes", page="views/routes.py")
title_slot = st.empty()  # filled in once the route is known
require_marts()
# the route's numbers and the hour-by-hour chart first, then the period and days
# filters (which everything on the page follows), then the rest (drawn in that order, though
# the filters are read first)
top = st.container()
start, wt = page_filters()
FV = str(current_fv())  # schedule version in force today
wt_clause, wt_params = day_sql(wt)
rank = route_rank(start, wt)
if rank.empty:
    with top:
        if lateness(start, None, "overall").empty:
            recomputing_note()
        else:
            st.info("No scored arrivals in the selected period yet.")
    st.stop()
with top:
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
    # the route comes from the page address (?route=...): every link to a route's report card
    # carries it
    route_id = st.query_params.get("route")
    if route_id not in ids:
        route_id = ids[0]
        st.query_params["route"] = route_id
    route_name = short[route_id]
    long_name = full[route_id].split(" — ", 1)[1] if " — " in full[route_id] else ""
    st.set_page_config(page_title=f"Route {full[route_id]}: how reliable? · Eugene Bus Watch")
    title_slot.title(
        f"How reliable is route {route_name}" + (f" ({long_name})" if long_name else "") + "?"
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

    # the route's numbers for the direction chosen above, like everything below them
    wt_m = day_sql(wt)[0]
    numbers_sql = f"""
        select count(*) as n,
               percentile_cont(0.5) within group (order by delay_s) as median_delay,
               percentile_cont(0.1) within group (order by delay_s) as p10_delay_s,
               percentile_cont(0.9) within group (order by delay_s) as p90_delay_s
        from marts.fct_stop_events
        where route_id = %s and status is not null {dir_clause} and {{when}}
    """
    me = q(
        numbers_sql.format(when=f"service_date >= %s {wt_m}"),
        (route_id, *dir_params, start, *wt_params),
    ).iloc[0]
    today = q(
        numbers_sql.format(when="service_date = %s"), (route_id, *dir_params, service_today())
    ).iloc[0]
    me["n_today"], me["median_today"] = today["n"], today["median_delay"]
    n_today = int(me["n_today"] or 0)
    with card():
        m1, m2, m3 = st.columns(3)
        m1.metric(
            "Typical bus vs timetable",
            fmt_delay(me["median_delay"]),
            help=TYPICAL_HELP
            + f" {int(me['n']):,} arrivals measured, {day_label(wt)} since {fmt_date(start)}.",
        )
        m2.metric("8 in 10 buses", fmt_range(me["p10_delay_s"], me["p90_delay_s"]), help=RANGE_HELP)
        m3.metric(
            "Today so far",
            fmt_delay(me["median_today"]) if n_today >= 10 else "—",
            help="The typical bus today, as of the latest update (every 15 minutes)"
            + (f", from {n_today:,} arrivals." if n_today else "; none measured yet."),
        )

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
    with card():
        st.subheader(
            "How close to the timetable, hour by hour?",
            help=LATENESS_CHART_NOTE
            + f" Every stop on route {route_name}, in the direction chosen "
            "above; the dashed grey line is the typical bus on all routes together.",
        )
        fig = lateness_chart(
            hourly,
            f"route {route_name}",
            reference=lateness(start, wt, "hour"),
            min_n=5,
        )
        if fig is None:
            st.caption("Not enough arrivals yet for an hour-by-hour view (needs 5 in an hour).")
        else:
            show_chart(fig)

with card():
    st.subheader(
        "Where does the delay build up?",
        help="Lateness against the timetable at each stop, in the order the bus reaches them (left "
        "to right). The line is the typical bus; the shaded band is where 8 in 10 buses fell. A "
        "line climbing means the bus loses time against the timetable there; falling means the "
        "timetable has slack there.",
    )
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
        if is_mobile():  # long stop names would leave little room for the plot on a phone
            along["Stop"] = [
                t if len(t) <= 20 else t[:19].rstrip() + "…" for t in along["Stop"].astype(str)
            ]
        along["p10_s"], along["p90_s"] = along["p10"].astype(float), along["p90"].astype(float)
        along["med_s"] = along["median_delay"].astype(float)
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
            xaxis={
                "type": "category",
                "categoryorder": "array",
                "categoryarray": stops_order,
                "tickangle": -45,
                "title": "",
            },
            yaxis=minutes_axis(list(along["p10"]) + list(along["p90"])),
            height=460 if is_mobile() else 520,
            legend={"orientation": "h", "yanchor": "bottom", "y": 1.02, "x": 0, "xanchor": "left"},
            margin={"l": 10, "r": 10, "t": 30, "b": 10},
        )
        show_chart(fig)

        # every stop on the route, sortable: by order along the route, typical bus or range
        lo, hi = range_scale(along, "p10_s", "p90_s")
        st.markdown("#### Every stop on this route")
        link_table(
            [
                {
                    "href": stop_link(r.stop_id, "").split("#")[0],
                    "hover": f"{r.stop_name}: {int(r.n):,} arrivals measured",
                    "seq": int(r.stop_sequence),
                    "stop": r.stop_name,
                    "typical": fmt_delay(r.median_delay * 60),
                    "typical_s": float(r.median_delay),
                    **range_fields(r.p10_s, r.median_delay * 60, r.p90_s, lo, hi),
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
            max_height=520,
            key="route_stops",
            default_sort="seq:asc",
        )
        st.caption("Click a stop for its report card; click a heading to sort.")

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


# ---- compare two routes -------------------------------------------------------------
with card():
    st.subheader("Compare two routes")
    other = st.selectbox(
        "Compare with", [r for r in ids if r != route_id], format_func=lambda r: full[r]
    )
    if other is None:
        st.caption(
            "Only one route has scored arrivals in this period, so there is nothing to compare."
        )
    else:
        # this route first, then the one it's compared with
        cmp = (
            rank[rank["route_id"].astype(str).isin([route_id, str(other)])]
            .assign(_first=lambda d: d["route_id"].astype(str) != route_id)
            .sort_values("_first")
            .set_index("route_short_name")
        )
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

# ---- the other routes --------------------------------------------------------------------
with card():
    st.subheader("Other routes' report cards")
    route_tiles(rank_sorted[rank_sorted["route_id"].astype(str) != route_id])

st.divider()
data_note(start)
