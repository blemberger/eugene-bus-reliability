"""Predictions: how much should you trust the countdown sign?"""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from common import (
    busy_stop_buttons,
    col_count,
    col_pct,
    col_time,
    current_fv,
    fmt_minutes,
    fmt_pct,
    hour_label,
    local_times,
    q,
    q_live,
    require_db,
    require_marts,
    route_picker,
    search_stops,
    share_pct,
    stop_buttons,
    table,
)

require_db()
st.title("How much should you trust the sign?")
st.caption(
    "LTD's feed predicts when each bus will reach each stop; that's what the countdown signs and apps show. "
    "We record every prediction and every revision, then compare each one to when the bus actually arrived."
)
require_marts()
FV = str(current_fv())  # schedule version in force today

routes = q(
    f"select route_id, route_short_name from gtfs.routes where feed_version_id = {FV} order by length(route_short_name), route_short_name"
)
route_id = route_picker(routes, key="pred_route")

cal = q(
    "select * from marts.mart_calibration where route_id is not distinct from %s order by horizon_min",
    (route_id,),
)
if cal.empty or cal["n_predictions"].sum() == 0:
    st.info(
        "No scored predictions yet. They appear after `make dbt-build` once observed arrivals exist."
    )
    st.stop()

# ---- headline ------------------------------------------------------------------------
at5 = cal[cal["horizon_min"] == 5]
at15 = cal[cal["horizon_min"] == 15]
c1, c2, c3 = st.columns(3)
if not at5.empty:
    c1.metric(
        "'5 minutes' is right within 1 min",
        fmt_pct(int(at5["n_within_1min"].iloc[0]), int(at5["n_predictions"].iloc[0])),
    )
if not at15.empty:
    c2.metric(
        "'15 minutes' is right within 2 min",
        fmt_pct(int(at15["n_within_2min"].iloc[0]), int(at15["n_predictions"].iloc[0])),
    )
c3.metric("Predictions scored", f"{int(cal['n_predictions'].sum()):,}")

# ---- calibration curve --------------------------------------------------------------
st.subheader("Accuracy by how far out the prediction is")
cal = cal[(cal["horizon_min"] <= 30) & (cal["n_predictions"] >= 30)]
if cal.empty:
    st.info(
        "No horizon has 30 scored predictions yet for this selection; the charts need more data."
    )
    st.stop()
custom = list(
    zip(
        cal["n_predictions"].astype(int),
        cal["n_days"].astype(int),
        cal["first_day"].astype(str),
        cal["last_day"].astype(str),
        strict=False,
    )
)
hover = (
    "%{x} min out: %{y:.0%}<br>%{customdata[0]:,} predictions over %{customdata[1]} day(s)"
    "<br>%{customdata[2]} to %{customdata[3]}<extra>%{fullData.name}</extra>"
)
fig = go.Figure()
for k, color in (("1", "#2e8b57"), ("2", "#1f77b4"), ("5", "#9467bd")):
    fig.add_trace(
        go.Scatter(
            x=cal["horizon_min"],
            y=cal[f"n_within_{k}min"] / cal["n_predictions"],
            mode="lines+markers",
            name=f"sign right within {k} min",
            line={"color": color, "width": 3},
            customdata=custom,
            hovertemplate=hover,
        )
    )
    fig.add_trace(
        go.Scatter(
            x=cal["horizon_min"],
            y=cal[f"n_schedule_within_{k}min"] / cal["n_predictions"],
            mode="lines",
            name=f"printed timetable within {k} min",
            line={"color": color, "dash": "dash", "width": 1.5},
            hovertemplate="%{x} min out: %{y:.0%}<extra>%{fullData.name}</extra>",
        )
    )
fig.update_layout(
    xaxis_title="minutes ahead the sign said the bus would arrive",
    yaxis_title="share of predictions that were right",
    yaxis_tickformat=".0%",
    yaxis_range=[0, 1],
    xaxis_range=[0, 30.5],
    legend_title="",
)
st.plotly_chart(fig, width="stretch")
st.caption(
    "Solid lines: how often the bus came within 1, 2 or 5 minutes of what the sign said, by how far ahead "
    "the sign said it. Dashed lines, same colours: for the same buses, how often the printed timetable was "
    "that close. Where a solid line is above its dashed twin, the live prediction is worth checking; where "
    "they meet, the timetable would have done as well. Click a legend entry to hide or show a line. "
    "Horizons with fewer than 30 scored predictions are left out."
)
st.markdown(
    "**How far ahead does LTD predict?** For every trip in progress, LTD's feed gives a time for each of "
    "the trip's remaining stops, so its predictions reach as far ahead as the end of the trip. This page "
    "shows up to 30 minutes, the range in which people use a countdown to decide when to leave; further "
    "ahead, people plan from the timetable. That cut is ours, not LTD's."
)

# ---- bias ------------------------------------------------------------------------------
st.subheader("Does the sign run optimistic or pessimistic?")
fig2 = go.Figure()
fig2.add_trace(
    go.Scatter(
        x=cal["horizon_min"],
        y=cal["p90_error_s"] / 60,
        mode="lines",
        line={"width": 0},
        showlegend=False,
        hoverinfo="skip",
    )
)
fig2.add_trace(
    go.Scatter(
        x=cal["horizon_min"],
        y=cal["p10_error_s"] / 60,
        mode="lines",
        line={"width": 0},
        fill="tonexty",
        fillcolor="rgba(31,119,180,0.15)",
        name="10th–90th percentile",
    )
)
fig2.add_trace(
    go.Scatter(
        x=cal["horizon_min"],
        y=cal["median_error_s"] / 60,
        mode="lines+markers",
        name="Typical (median)",
        line={"color": "#1f77b4"},
        customdata=custom,
        hovertemplate="%{x} min out: %{y:+.1f} min<br>%{customdata[0]:,} predictions over %{customdata[1]} day(s)<extra></extra>",
    )
)
fig2.add_hline(y=0, line_dash="dot", line_color="grey")
fig2.update_layout(
    xaxis_title="minutes ahead",
    yaxis_title="bus arrived this many minutes AFTER the sign said",
    legend_title="",
)
st.plotly_chart(fig2, width="stretch")
st.caption(
    "Above zero: the bus came later than predicted (the sign was optimistic). Below: earlier (you might miss it)."
)
late_share = cal["n_bus_later_than_sign"].sum() / cal["n_predictions"].sum()
early_share = cal["n_bus_earlier_than_sign"].sum() / cal["n_predictions"].sum()
st.write(
    f"Across all horizons, the bus came more than a minute **later** than the sign {late_share:.0%} of the time and more than a minute **earlier** {early_share:.0%} of the time."
)

# ---- by route at 5 and 10 min ------------------------------------------------------------
if route_id is None:
    st.subheader("Which routes have the best predictions?")
    by_route = q(f"""
        select r.route_short_name as route,
               sum(c.n_predictions) filter (where c.horizon_min = 5)  as n5,
               sum(c.n_within_1min) filter (where c.horizon_min = 5)  as w5,
               sum(c.n_predictions) filter (where c.horizon_min = 10) as n10,
               sum(c.n_within_2min) filter (where c.horizon_min = 10) as w10,
               sum(c.n_predictions) as n
        from marts.mart_calibration c
        join gtfs.routes r on r.route_id = c.route_id and r.feed_version_id = {FV}
        where c.route_id is not null
        group by 1 having sum(c.n_predictions) >= 100
        order by sum(c.n_within_1min) filter (where c.horizon_min = 5)::float / nullif(sum(c.n_predictions) filter (where c.horizon_min = 5), 0) desc nulls last
    """)
    if not by_route.empty:
        table(
            pd.DataFrame(
                {
                    "Route": by_route["route"].to_numpy(),
                    "'5 min' right within 1 min": share_pct(
                        by_route["w5"], by_route["n5"]
                    ).to_numpy(),
                    "'10 min' right within 2 min": share_pct(
                        by_route["w10"], by_route["n10"]
                    ).to_numpy(),
                    "Predictions": by_route["n"].astype(int).to_numpy(),
                }
            ),
            hide_index=True,
            width="stretch",
            column_config={
                "'5 min' right within 1 min": col_pct("'5 min' right within 1 min", bar=True),
                "'10 min' right within 2 min": col_pct("'10 min' right within 2 min", bar=True),
                "Predictions": col_count("Predictions"),
            },
        )

# ---- by time of day ---------------------------------------------------------------------
st.subheader("Does accuracy depend on the time of day?")
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
tod = q(
    """
    select hour_local, count(*) as n,
           count(*) filter (where abs(error_s) <= 60) as within1,
           count(*) filter (where abs(error_s) <= 120) as within2,
           count(*) filter (where abs(error_s) <= 180) as within3
    from marts.fct_prediction_errors
    where horizon_min between %s and %s and (%s::text is null or route_id = %s)
    group by 1 order by 1
    """,
    (lo, hi, route_id, route_id),
)
tod = tod[tod["n"] >= 20].copy() if not tod.empty else tod
if tod.empty:
    st.caption("Not enough scored predictions this far ahead yet (needs 20 in an hour).")
else:
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
                    "<br>%{customdata[3]:,} predictions<extra></extra>"
                ),
            )
        )
    fig3.update_layout(
        barmode="stack",
        yaxis_tickformat=".0%",
        yaxis_range=[0, 1],
        xaxis_title="",
        yaxis_title=f"'{ahead}' predictions that were right",
        legend={
            "orientation": "h",
            "yanchor": "bottom",
            "y": 1.02,
            "x": 0,
            "title": "",
            "traceorder": "normal",
        },
    )
    st.plotly_chart(fig3, width="stretch")
    st.caption(
        f"Predictions made about {ahead} ahead, by the hour they were made. Each bar builds up: the "
        "dark part is the share that was right to within 1 minute; add the middle part for within 2 "
        "minutes; the whole bar is within 3 minutes. The empty space above is how often the bus came "
        "more than 3 minutes off the prediction. Hours with fewer than 20 predictions are left out."
    )

# ---- revisions --------------------------------------------------------------------------
st.subheader("How often does the prediction change?")
rev = q("""
    with per_stop as (
        select count(*) as revisions,
               extract(epoch from max(last_seen_at) - min(first_seen_at)) as tracked_s
        from (
            select trip_id, start_date, stop_sequence, first_seen_at, last_seen_at from rt.prediction_history
            union all
            select trip_id, start_date, stop_sequence, first_seen_at, last_seen_at from rt.prediction_current
        ) u
        where start_date >= current_date - 7
        group by trip_id, start_date, stop_sequence
    )
    select count(*) as trip_stops,
           percentile_cont(0.5) within group (order by revisions) as median_revisions,
           percentile_cont(0.9) within group (order by revisions) as p90_revisions,
           percentile_cont(0.5) within group (order by tracked_s) as median_tracked_s
    from per_stop
""")
if not rev.empty and rev["trip_stops"][0]:
    r = rev.iloc[0]
    st.write(
        f"Over the last 7 days, a typical stop's prediction was revised **{int(r['median_revisions'])} times** "
        f"(1 in 10 stops: {int(r['p90_revisions'])} or more) while it was on the board for about **{fmt_minutes(r['median_tracked_s'])}**."
    )
st.caption(
    "Every revision is kept; that's what lets a prediction be scored at the horizon it was made, not just the last value shown."
)

# ---- one stop, right now: how each coming bus's prediction has been revised --------------
st.subheader("One stop, right now")
st.caption(
    "Pick a stop; for each bus coming, see how its predicted arrival has been revised since it first appeared."
)
example = q(f"""
    select stop_code, stop_name from gtfs.stops
    where feed_version_id = {FV} and location_type = 0 and stop_code is not null and stop_code <> ''
    order by stop_name limit 1
""")
hint = (
    f"e.g. {example['stop_name'][0]}, or {example['stop_code'][0]}"
    if not example.empty
    else "stop name or number"
)
query = st.text_input(
    "Stop name or the number on the sign", placeholder=hint, key="pred_stop_query"
)
if not query:
    st.write("Or start with a busy stop:")
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
    st.markdown(f"**{st.session_state.get('pred_stop_name', pstop)}**")
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
            xaxis_title="when the feed said it",
            yaxis_title="predicted arrival at this stop",
            legend_title="",
            height=420,
        )
        st.plotly_chart(fig4, width="stretch")
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
            "A flat line is a prediction that held; a line that keeps stepping later is the sign being optimistic and correcting itself as the bus falls behind."
        )
