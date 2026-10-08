"""Countdown: how far can you trust LTD's live countdown, and how does it compare with the
timetable?"""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from common import (
    COUNTDOWN_OFF_NOTE,
    busy_stop_buttons,
    card,
    col_count,
    col_time,
    countdown_off,
    countdown_off_chart,
    current_fv,
    fit_phone,
    fmt_date,
    fmt_minutes,
    fmt_pct,
    hour_label,
    link_table,
    local_times,
    q,
    q_live,
    require_db,
    require_marts,
    route_picker,
    search_stops,
    service_hour_key,
    stop_buttons,
    table,
)

require_db()
st.title("Can you trust the countdown?")
st.caption(
    "LTD's live countdown, on bus-stop signs and in transit apps, against when the bus came."
)
require_marts()
FV = str(current_fv())  # schedule version in force today

routes = q(
    f"select route_id, route_short_name from gtfs.routes where feed_version_id = {FV} order by length(route_short_name), route_short_name"
)
route_id = route_picker(routes, key="pred_route")

# The countdown at each distance, one count per bus arrival, on the same arrivals as the
# timetable (marts.mart_told_vs_actual, from fct_countdown_samples). n_predictions here is a
# number of bus arrivals.
try:
    cal = q(
        """
        select ahead_min as horizon_min, n as n_predictions, n_within_1min, n_within_2min,
               n_days, first_day, last_day
        from marts.mart_told_vs_actual
        where basis = 'sign' and stop_id is null and route_id is not distinct from %s
        order by 1
        """,
        (route_id,),
    )
except Exception:  # noqa: BLE001 — the mart is being rebuilt with its new columns
    cal = pd.DataFrame()
if cal.empty or cal["n_predictions"].sum() == 0:
    st.info("The countdown numbers are being recalculated; they reappear within about 20 minutes.")
    st.stop()

# The timetable, on the same bus arrivals as the countdown (those with a countdown 15 minutes
# out; fct_countdown_samples): one value, since the timetable doesn't change as the bus nears.
TT = q(
    """
    select route_id, n, n_within_1min, n_within_2min from marts.mart_told_vs_actual
    where basis = 'timetable' and stop_id is null
    """
)
tt_share = {
    (None if pd.isna(r.route_id) else str(r.route_id)): (
        r.n_within_1min / r.n if r.n else None,
        r.n_within_2min / r.n if r.n else None,
    )
    for r in TT.itertuples()
}
tt1, tt2 = tt_share.get(route_id, (None, None))

# ---- headline ------------------------------------------------------------------------
at5 = cal[cal["horizon_min"] == 5]
at15 = cal[cal["horizon_min"] == 15]
c1, c2 = st.columns(2)
N_CMP = (
    TT[TT["route_id"].isna()]["n"].sum()
    if route_id is None
    else (TT[TT["route_id"].astype(str) == str(route_id)]["n"].sum())
)
SCORED = (
    f" Both measured on the same {int(N_CMP):,} bus arrivals: those with a countdown 15 "
    "minutes out."
)
if not at5.empty:
    r5 = at5.iloc[0]
    c1.metric(
        "Countdown says 5 min: bus comes within 1 min of that",
        fmt_pct(int(r5["n_within_1min"]), int(r5["n_predictions"])),
        help=(
            f"The printed timetable is right to within 1 minute {tt1:.0%} of the time."
            if tt1 is not None
            else ""
        )
        + SCORED,
    )
if not at15.empty:
    r15 = at15.iloc[0]
    c2.metric(
        "Countdown says 15 min: bus comes within 2 min of that",
        fmt_pct(int(r15["n_within_2min"]), int(r15["n_predictions"])),
        help=(
            f"The printed timetable is right to within 2 minutes {tt2:.0%} of the time."
            if tt2 is not None
            else ""
        )
        + SCORED,
    )

# ---- how far off: the countdown at each distance, every route, and the timetable ----------
with card():
    st.subheader(
        "How far off is the countdown?",
        help=COUNTDOWN_OFF_NOTE
        + (
            f" All data since {fmt_date(cal['first_day'].min())}."
            if "first_day" in cal and len(cal)
            else ""
        ),
    )
    fig0 = countdown_off_chart(
        countdown_off(),
        dict(
            zip(
                routes["route_id"].astype(str), routes["route_short_name"].astype(str), strict=False
            )
        ),
        focus=route_id,
        focus_label="All routes"
        if route_id is None
        else f"Route {routes.set_index('route_id')['route_short_name'].astype(str).get(route_id, route_id)}",
        min_n=30,
    )
    if fig0 is None:
        st.caption("Not enough measured arrivals yet, or the numbers are being recalculated.")
    else:
        st.plotly_chart(fit_phone(fig0), width="stretch")


# ---- by time of day ---------------------------------------------------------------------
with card():
    st.subheader(
        "Does accuracy depend on the time of day?",
        help="Predictions made about this far ahead, by the hour they were made. Each bar builds "
        "up: the dark part is the share right to within 1 minute; add the middle part for within "
        "2 minutes; the whole bar is within 3 minutes. The space above is how often the bus came "
        "more than 3 minutes off. Hours with fewer than 20 predictions are left out.",
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
    tod = (
        q(
            """
        select hour_local, sum(n)::bigint as n, sum(n_within_1min)::bigint as within1,
               sum(n_within_2min)::bigint as within2, sum(n_within_3min)::bigint as within3
        from marts.mart_accuracy_by_hour
        where horizon_min between %s and %s and route_id is not distinct from %s
        group by 1 order by 1
        """,
            (lo, hi, route_id),
        )
        if q("select to_regclass('marts.mart_accuracy_by_hour') is not null as ok")["ok"][0]
        else pd.DataFrame()
    )
    tod = tod[tod["n"] >= 20].copy() if not tod.empty else tod
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
        st.plotly_chart(fit_phone(fig3), width="stretch")


# ---- by route at 5 and 10 min ------------------------------------------------------------
if route_id is None:
    with card():
        st.subheader("Which routes have the best predictions?")
        by_route = q(f"""
            select c.route_id, r.route_short_name as route,
                   sum(c.n) filter (where c.ahead_min = 5)  as n5,
                   sum(c.n_within_1min) filter (where c.ahead_min = 5)  as w5,
                   sum(c.n) filter (where c.ahead_min = 10) as n10,
                   sum(c.n_within_2min) filter (where c.ahead_min = 10) as w10,
                   sum(c.n) filter (where c.ahead_min = 15) as n
            from marts.mart_told_vs_actual c
            join gtfs.routes r on r.route_id = c.route_id and r.feed_version_id = {FV}
            where c.basis = 'sign' and c.route_id is not null and c.stop_id is null
            group by 1, 2 having sum(c.n) filter (where c.ahead_min = 15) >= 50
        """)
        if not by_route.empty:

            def share(k, n) -> float | None:
                return None if not n or pd.isna(n) else float(k) / float(n)

            def bar(v: float | None, colour: str) -> str:
                if v is None:
                    return ""
                return (
                    "<span class='ebw-g'><span class='ebw-x' style='width:80px'>"
                    f"<span class='ebw-x1' style='left:0;top:4px;width:{v * 100:.0f}%;"
                    f"background:{colour}'></span></span><span>{v:.0%}</span></span>"
                )

            rows = []
            for r in by_route.itertuples():
                c5, c10 = share(r.w5, r.n5), share(r.w10, r.n10)
                t5, t10 = tt_share.get(str(r.route_id), (None, None))
                rows.append(
                    {
                        "href": f"/accuracy?route={r.route_id}",
                        "hover": f"Route {r.route}: measured on {int(r.n):,} bus arrivals",
                        "route": r.route,
                        "c5": bar(c5, "#1f5f9e"),
                        "c5_s": c5,
                        "c10": bar(c10, "#1f5f9e"),
                        "c10_s": c10,
                        "t5": bar(t5, "#9a9a9a"),
                        "t5_s": t5,
                        "t10": bar(t10, "#9a9a9a"),
                        "t10_s": t10,
                    }
                )
            cols = [{"key": "route", "label": "Route", "width": "3.4em", "bold": True}]
            for k, label, tip in (
                (
                    "c5",
                    "Countdown at 5 min: within 1 min",
                    "When the countdown said 5 minutes, how often the bus came within 1 minute "
                    "of that.",
                ),
                (
                    "c10",
                    "Countdown at 10 min: within 2 min",
                    "When the countdown said 10 minutes, how often the bus came within 2 minutes "
                    "of that.",
                ),
                (
                    "t5",
                    "Timetable: within 1 min",
                    "How often this route's printed timetable is right to within 1 minute, over "
                    "the same bus arrivals.",
                ),
                (
                    "t10",
                    "Timetable: within 2 min",
                    "How often this route's printed timetable is right to within 2 minutes, over "
                    "the same bus arrivals.",
                ),
            ):
                cols.append(
                    {
                        "key": k,
                        "label": label,
                        "width": "minmax(8em, 1fr)",
                        "html": True,
                        "help": tip,
                        "sort": f"{k}_s",
                        "first": "desc",
                        "hide_on_phone": k in ("t5", "t10"),
                    }
                )
            link_table(rows, cols, max_height=640, key="pred_routes", default_sort="c5:desc")
            st.caption("Click a route to see this page for that route alone.")


# ---- one stop, right now: how each coming bus's prediction has been revised --------------
with card():
    st.subheader("One stop, right now")
    st.caption("Pick a stop to see how each coming bus's predicted arrival has changed.")
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
            st.plotly_chart(fit_phone(fig4), width="stretch")
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
                "A flat line is a prediction that held; a line that keeps stepping later is the countdown being optimistic and correcting itself as the bus falls behind."
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
