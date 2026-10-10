"""Predictions: how far can you trust LTD's real-time arrival predictions, and how do they compare with the
timetable?"""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from common import (
    ALL_ROUTES,
    PREDICTION_OFF_CAPTION,
    PREDICTION_OFF_NOTE,
    busiest_route,
    busy_stop_buttons,
    card,
    col_count,
    col_time,
    countdown_off_at_stop,
    countdown_off_chart,
    current_fv,
    day_label,
    day_sql,
    fmt_date,
    fmt_minutes,
    hour_label,
    link_table,
    local_times,
    page_filters,
    prediction_off,
    q,
    q_live,
    require_db,
    require_marts,
    route_picker,
    route_toggles,
    search_stops,
    service_hour_key,
    show_chart,
    stop_buttons,
    table,
)

require_db()
st.title("Can you trust the predictions?")
st.caption(
    "LTD's real-time arrival predictions (what apps like Transit show), against when the bus "
    "actually came."
)
require_marts()
FV = str(current_fv())  # schedule version in force today

routes = q(
    f"select route_id, route_short_name from gtfs.routes where feed_version_id = {FV} order by length(route_short_name), route_short_name"
)
short = dict(
    zip(routes["route_id"].astype(str), routes["route_short_name"].astype(str), strict=False)
)

# the first chart, then the period and days filters that everything on the page follows (drawn
# in that order, though the filters are read first)
chart_slot = st.container()
start, wt = page_filters()
period = f"{day_label(wt)} since {fmt_date(start)}"

# Average minutes off for the prediction at each distance and the timetable, on the same bus
# arrivals, each counted once per distance (marts.mart_prediction_daily): all routes together
# and each route, for the chosen period and days.
off = prediction_off(start, wt)
if off.empty or off["n"].sum() == 0:
    with chart_slot:
        st.info(
            "No measured predictions for this period yet, or the numbers are being "
            "recalculated (they reappear within about 20 minutes)."
        )
    st.stop()
with_data = sorted({str(r) for r in off["route_id"].dropna()})

# ---- how far off: the prediction at each distance, by route, and the timetable ----------------
with chart_slot, card():
    st.subheader(
        "How far off are the predictions?",
        help=PREDICTION_OFF_NOTE + f" {period[0].upper() + period[1:]}.",
    )
    # every route together, and one route to compare: the one asked for in the address (a row
    # of the routes table below), else the route with the most bus arrivals
    wanted = st.query_params.get("route")
    compare = wanted if wanted in with_data else busiest_route(off)
    lines = route_toggles(
        with_data, short, [ALL_ROUTES, *([compare] if compare else [])], key="pred_lines"
    )
    fig0 = countdown_off_chart(off, short, lines, min_n=30)
    if fig0 is None:
        st.caption("Pick a route above (or All routes); this one needs more measured arrivals.")
    else:
        show_chart(fig0)
        st.caption(PREDICTION_OFF_CAPTION)


# ---- by time of day ---------------------------------------------------------------------
with card():
    title_slot = st.empty()  # the title names the route picked just below it
    tod_route = route_picker(
        routes[routes["route_id"].astype(str).isin(with_data)], key="tod_route"
    )
    scope = "All routes" if tod_route is None else f"Route {short.get(str(tod_route), tod_route)}"
    title_slot.subheader(
        f"Does accuracy depend on the time of day? {scope}, {day_label(wt)}",
        help="Predictions made about this far ahead, by the hour they were on show. Each bar "
        "builds up: the dark part is the share right to within 1 minute; add the middle part "
        "for within 2 minutes; the whole bar is within 3 minutes. The space above is how often "
        "the bus came more than 3 minutes off. Hours with fewer than 20 bus arrivals are left "
        f"out. {period[0].upper() + period[1:]}.",
    )
    AHEAD_CHOICES = {"2 min": (1, 2), "5 min": (5, 5), "10 min": (10, 10), "15 min": (15, 15)}
    ahead = (
        st.segmented_control(
            "Predictions made this far ahead",
            list(AHEAD_CHOICES),
            default="5 min",
            key="tod_ahead",
        )
        or "5 min"
    )
    lo, hi = AHEAD_CHOICES[ahead]
    clause, params = day_sql(wt)
    tod = q(
        f"""
        select hour_local, sum(n)::bigint as n, sum(n_within_1min)::bigint as within1,
               sum(n_within_2min)::bigint as within2, sum(n_within_3min)::bigint as within3
        from marts.mart_prediction_daily
        where basis = 'sign' and ahead_min between %s and %s and service_date >= %s {clause}
          {"and route_id = %s" if tod_route else ""}
        group by 1 order by 1
        """,
        (lo, hi, start, *params, *([tod_route] if tod_route else [])),
    )
    tod = tod[tod["n"] >= 20].copy()
    if tod.empty:
        st.caption("Not enough measured bus arrivals this far ahead yet (needs 20 in an hour).")
    else:
        # service order: 5 am first, the hours after midnight last
        tod = tod.assign(_k=tod["hour_local"].map(service_hour_key)).sort_values("_k")
        tod["Hour"] = tod["hour_local"].map(hour_label)
        for k in (1, 2, 3):
            tod[f"p{k}"] = tod[f"within{k}"] / tod["n"]
        # One bar per hour, built up in layers: the dark part is "right within 1 min", adding the
        # middle part gives "within 2 min", the whole bar "within 3 min". One blue, dark to light.
        layers = [
            ("p1", None, "within 1 min", "#1f5f9e"),
            ("p2", "p1", "1–2 min off", "#5b9bd5"),
            ("p3", "p2", "2–3 min off", "#b3d1ee"),
        ]
        custom = list(zip(tod["p1"], tod["p2"], tod["p3"], tod["n"].astype(int), strict=False))
        fig3 = go.Figure()
        for col, below, name, color in layers:
            fig3.add_trace(
                go.Bar(
                    x=tod["Hour"],
                    y=tod[col] - (tod[below] if below else 0),
                    name=name,
                    marker={"color": color, "line": {"color": "white", "width": 1}},
                    customdata=custom,
                    hovertemplate=(
                        "<b>%{x}</b><br>right within 1 min: %{customdata[0]:.0%}<br>"
                        "within 2 min: %{customdata[1]:.0%}<br>within 3 min: %{customdata[2]:.0%}"
                        "<br>%{customdata[3]:,} bus arrivals<extra></extra>"
                    ),
                )
            )
        fig3.update_layout(
            barmode="stack",
            yaxis_tickformat=".0%",
            yaxis_range=[0, 1],
            xaxis_title="",
            yaxis_title=f"Predictions {ahead} out that were right",
            legend={
                "orientation": "h",
                "yanchor": "bottom",
                "y": 1.02,
                "x": 0,
                "title": "",
                "traceorder": "normal",
            },
        )
        show_chart(fig3)


# ---- by route: average minutes off at 5 and 10 minutes away, and the timetable -------------
with card():
    st.subheader(
        f"Which routes have the best predictions? {day_label(wt).capitalize()}",
        help="Average minutes the bus came from the time it was given, early or late alike, on "
        "the same bus arrivals: the prediction when it said 5 and 10 minutes away, and the "
        "printed timetable. Lower is better. Routes with at least 50 bus arrivals measured. "
        f"{period[0].upper() + period[1:]}. Click a column heading to sort; click a route to "
        "show it in the chart at the top.",
    )
    per = off[off["route_id"].notna()].copy()
    per["route_id"] = per["route_id"].astype(str)
    at = {
        k: per[(per["basis"] == "sign") & (per["ahead_min"] == k)].set_index("route_id")
        for k in (5, 10, 15)
    }
    tt = per[per["basis"] == "timetable"].set_index("route_id")
    ids = [r for r in tt.index if r in at[15].index and at[15].loc[r, "n"] >= 50]
    if not ids:
        st.caption("Not enough measured bus arrivals per route yet for this period.")
    else:

        def minutes(frame: pd.DataFrame, r: str) -> float | None:
            if r not in frame.index or pd.isna(frame.loc[r, "mean_abs_s"]):
                return None
            return float(frame.loc[r, "mean_abs_s"]) / 60

        values = {r: (minutes(at[5], r), minutes(at[10], r), minutes(tt, r)) for r in ids}
        top = max(v for vs in values.values() for v in vs if v is not None)

        def bar(v: float | None, colour: str) -> str:
            if v is None:
                return ""
            return (
                "<span class='ebw-g'><span class='ebw-x'>"
                f"<span class='ebw-x1' style='left:0;top:4px;width:{v / top * 100:.0f}%;"
                f"background:{colour}'></span></span><span class='ebw-s'>{v:.1f} min</span>"
                "</span>"
            )

        rows = []
        for r in sorted(ids, key=lambda r: (len(short.get(r, r)), short.get(r, r))):
            c5, c10, t = values[r]
            rows.append(
                {
                    "href": f"/accuracy?route={r}",
                    "hover": f"Route {short.get(r, r)}: measured on "
                    f"{int(at[15].loc[r, 'n']):,} bus arrivals",
                    "route": short.get(r, r),
                    "c5": bar(c5, "#1f5f9e"),
                    "c5_s": c5,
                    "c10": bar(c10, "#1f5f9e"),
                    "c10_s": c10,
                    "tt": bar(t, "#8a8f94"),
                    "tt_s": t,
                }
            )
        cols = [{"key": "route", "label": "Route", "width": "3.4em", "bold": True}]
        for k, label, tip in (
            (
                "c5",
                "Prediction, 5 min away",
                "Average minutes off when the prediction said 5 minutes.",
            ),
            (
                "c10",
                "Prediction, 10 min away",
                "Average minutes off when the prediction said 10 minutes.",
            ),
            (
                "tt",
                "Printed timetable",
                "Average minutes off the printed timetable, same bus arrivals.",
            ),
        ):
            cols.append(
                {
                    "key": k,
                    "label": label,
                    "width": "minmax(9em, 1fr)",
                    "html": True,
                    "help": tip,
                    "sort": f"{k}_s",
                    "first": "asc",
                }
            )
        link_table(
            rows,
            cols,
            max_height=640,
            key="pred_routes",
            default_sort="c5:asc",
            go_label="Show",
        )


# ---- one stop, right now: how each coming bus's prediction has been revised --------------
with card():
    st.subheader(
        "One stop",
        help="How far off the predictions are at one stop, by route (for the period and days "
        "above), and how each bus coming there now has had its predicted arrival changed.",
    )
    example = q(f"""
        select stop_code, stop_name from gtfs.stops
        where feed_version_id = {FV} and location_type = 0 and stop_code is not null and stop_code <> ''
        order by stop_name limit 1
    """)
    hint = (
        f"stop name or the number on the sign, e.g. {example['stop_name'][0]} or "
        f"{example['stop_code'][0]}"
        if not example.empty
        else "stop name or number"
    )
    query = st.text_input(
        "Stop name or the number on the sign",
        placeholder="Search: " + hint,
        key="pred_stop_query",
        label_visibility="collapsed",
    )
    if not query:
        picked = busy_stop_buttons("pred_busy")
        if picked:
            st.session_state["pred_stop_id"] = picked
            st.session_state["pred_stop_name"] = st.session_state.get("pred_busy_name", picked)
    if query:
        matches = search_stops(query)
        if matches.empty:
            st.warning("No stop matches that. Try part of a street name.")
        else:
            picked = stop_buttons(matches, "pred_stop")
            if picked:
                st.session_state["pred_stop_id"] = picked

    pstop = st.session_state.get("pred_stop_id")
    if pstop:
        st.markdown(f"#### {st.session_state.get('pred_stop_name', pstop)}")
        st.page_link(
            "views/stops.py", label="This stop's report card →", query_params={"stop": pstop}
        )
        # how far off the predictions are here, by route (the same chart as at the top)
        at_stop = countdown_off_at_stop(pstop, start, wt)
        here_ids = sorted({str(r) for r in at_stop["route_id"].dropna()})
        # every route here together, and (where several stop here) the busiest one to compare
        busiest = busiest_route(at_stop) if len(here_ids) > 1 else None
        here_lines = route_toggles(
            here_ids,
            short,
            [ALL_ROUTES, *([busiest] if busiest else [])],
            key=f"pred_stop_lines_{pstop}",
            all_label="All routes here",
        )
        fig_s = countdown_off_chart(at_stop, short, here_lines, all_label="All routes here")
        if fig_s is None:
            st.caption("Not enough measured arrivals with a prediction here yet for this period.")
        else:
            show_chart(fig_s)
        st.markdown("**Right now: how each coming bus's predicted arrival has changed**")
        hist = q_live(
            f"""
            with coming as (
                select trip_id, start_date, stop_sequence from rt.prediction_current
                where stop_id = %s and coalesce(arrival_time, departure_time) between now() - interval '2 minutes' and now() + interval '45 minutes'
                  and last_seen_at > now() - interval '3 minutes'
            )
            select u.trip_id, r.route_short_name as route, t.trip_headsign as headsign,
                   u.first_seen_at, u.last_seen_at, coalesce(u.arrival_time, u.departure_time) as predicted
            from (
                select trip_id, start_date, stop_sequence, arrival_time, departure_time, first_seen_at, last_seen_at from rt.prediction_history
                union all
                select trip_id, start_date, stop_sequence, arrival_time, departure_time, first_seen_at, last_seen_at from rt.prediction_current
            ) u
            join coming c using (trip_id, start_date, stop_sequence)
            join gtfs.trips t  on t.trip_id = u.trip_id and t.feed_version_id = {FV}
            join gtfs.routes r on r.route_id = t.route_id and r.feed_version_id = {FV}
            where coalesce(u.arrival_time, u.departure_time) is not null
            order by u.trip_id, u.first_seen_at
            """,
            (pstop,),
        )
        if hist.empty:
            st.info("No buses predicted for this stop in the next 45 minutes.")
        else:
            hist["Bus"] = (
                "Route "
                + hist["route"].astype(str)
                + " · "
                + hist["headsign"].fillna("").astype(str)
                + " · trip "
                + hist["trip_id"].astype(str)
            )
            hist["asserted"] = pd.to_datetime(hist["first_seen_at"], utc=True).dt.tz_convert(
                "America/Los_Angeles"
            )
            hist["predicted_local"] = pd.to_datetime(hist["predicted"], utc=True).dt.tz_convert(
                "America/Los_Angeles"
            )
            fig4 = go.Figure()
            for bus, g in hist.groupby("Bus"):
                fig4.add_trace(
                    go.Scatter(
                        x=g["asserted"],
                        y=g["predicted_local"],
                        mode="lines+markers",
                        name=bus,
                        line={"shape": "hv"},
                    )
                )
            fig4.update_layout(
                xaxis_title="When LTD said it",
                yaxis_title="Predicted arrival at this stop",
                legend_title="",
                height=420,
            )
            show_chart(fig4)
            latest = hist.sort_values("first_seen_at").groupby("Bus", as_index=False).last()
            table(
                pd.DataFrame(
                    {
                        "Bus": latest["Bus"].to_numpy(),
                        "Now predicted": local_times(latest["predicted"]).to_numpy(),
                        "First predicted": local_times(
                            hist.sort_values("first_seen_at")
                            .groupby("Bus")["predicted"]
                            .first()
                            .reindex(latest["Bus"])
                        ).to_numpy(),
                        "Revisions": hist.groupby("Bus").size().reindex(latest["Bus"]).to_numpy(),
                    }
                ),
                hide_index=True,
                width="stretch",
                column_config={
                    "Now predicted": col_time("Now predicted"),
                    "First predicted": col_time("First predicted"),
                    "Revisions": col_count("Revisions"),
                },
            )
            st.caption(
                "A flat line is a prediction that held; a line that keeps stepping later is the prediction being optimistic and correcting itself as the bus falls behind."
            )


# ---- revisions --------------------------------------------------------------------------
with card():
    st.subheader(
        "How often does the prediction change?",
        help="Every revision is kept; that's what lets a prediction be scored at the distance it "
        "was made, not just the last value shown.",
    )
    # computed once per analysis build (marts.mart_prediction_revisions): it reads every
    # prediction of the week, far too slow to run on each visit
    has_rev = q("select to_regclass('marts.mart_prediction_revisions') is not null as ok")["ok"][0]
    rev = q("select * from marts.mart_prediction_revisions") if has_rev else pd.DataFrame()
    if not rev.empty and rev["trip_stops"][0]:
        r = rev.iloc[0]
        st.write(
            f"Over the last 7 days, a typical stop's prediction was revised **{int(r['median_revisions'])} times** "
            f"(1 in 10 stops: {int(r['p90_revisions'])} or more) while it was on the board for about **{fmt_minutes(r['median_tracked_s'])}**."
        )
