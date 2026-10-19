"""Data & methods: how the site measures lateness, how much of the service it measured, and the
health of LTD's feed right now."""

from __future__ import annotations

import math

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from common import (
    LINE_MAIN,
    MARKER_MAIN,
    arrow_safe,
    card,
    col_count,
    col_date,
    col_minutes,
    collection_start,
    fmt_date,
    fmt_pct,
    local_today,
    marts_ready,
    q,
    require_db,
    show_chart,
)

from eugene_bus_reliability.faq import questions

require_db()
st.title("Data & methods")
st.caption("How the numbers on this site are made, and how complete they are.")

# ---- definitions -------------------------------------------------------------------------
with card():
    st.subheader("How it's measured")
    start = collection_start()
    st.markdown(f"""
**Sources.** Lane Transit District's public GTFS schedule and GTFS-Realtime feeds (TripUpdates, VehiclePositions, Alerts),
polled every 30 seconds since {fmt_date(start) if start else "—"}. LTD is the source of all schedule and vehicle data;
this project is independent and not affiliated with LTD.

**Lateness.** For every bus at every stop: the time it actually arrived minus the time the printed timetable gives for
that stop. Positive is late, negative is early. Every stop counts, not only the timepoints, because riders wait at
every stop.

**Timepoints.** On each route, a few main stops have an exact time in LTD's timetable (the times printed in its
schedules); a bus that gets to one early waits there until its time. Every other stop's time is LTD's own estimate
between them.

**Typical bus.** The median lateness: half the buses were later than this, half earlier.

**8 in 10 buses.** The range from the 10th to the 90th percentile of lateness: one bus in ten came earlier than the
low end and one in ten later than the high end. Its width is how much you need to allow for when planning.

**Minutes off.** How far the bus was from the timetable, early or late alike, averaged: a route whose buses are
2 minutes early half the time and 2 minutes late the rest is 2 minutes off, though its typical bus is on time.

**Today so far.** The typical bus over today's service, as of the latest update (every 15 minutes).

**Prediction accuracy.** Every real-time arrival prediction LTD publishes is kept, with when it was shown.
"5 minutes away" means the prediction said 5:00 to 5:59. Each bus arrival counts once at each distance (the
prediction on show at that moment), and the timetable is measured on the same arrivals. As with lateness, the
charts show the typical bus and the range 8 in 10 buses came in against the predicted time, later or earlier; the
tables also give minutes off, how far the actual arrival was from the predicted one, early or late alike, averaged.

**Observed arrival.** LTD's vehicle positions carry no stop information, so arrivals are derived geometrically: each
position is placed along the route's shape as a fraction of the way along it (walked in time order, each position on
the first stretch of the route ahead of the last, so a route that uses a street twice can't be confused), the running
maximum over time removes GPS jitter, and when that fraction steps past a stop between two reports the crossing time is
interpolated (±15 s at a 30 s poll while moving). Independently, the feed keeps reporting a departure time for stops
already passed that no longer changes; that "settled" time is recorded too, and the two are compared in the cross-check
below. Stops passed before a trip's first report can't be timed and are excluded. What counts is when the bus leaves. At a
trip's first stop and at timepoints, where an early bus waits, our GPS time can catch the bus still waiting, and LTD's
own recorded departure is sometimes just the scheduled time, so there the later of the two is used. Everywhere else
our GPS time is used, which in practice is when the bus goes past or pulls away.

**Headways.** On routes scheduled every 20 minutes or better, the gap between consecutive buses at a stop is compared to
the scheduled gap. Bunched = under half the scheduled gap; big gap = over 1.5×.

**Excluded.** Trips with no realtime data; stops with no bounded observed arrival; arrivals more than 30 minutes early or
90 minutes late against the schedule, which in practice means a bus reporting the wrong trip rather than real service
(counted as "Implausible" in the coverage table below); arrivals whose time can't be pinned down to within 2 minutes
because the bus's reports around that stop were far apart, usually during a feed outage ("Imprecise" below);
stops where, trip after trip, our GPS time is more than 2 minutes out of line with the stops just before and after
it, usually a stop whose mapped position isn't where buses really stop ("Doubtful stop" below);
predictions more than 90 minutes out or made after the bus had arrived; the first stop of a trip for headway purposes.

**Known failure modes we measure rather than assume.** Feed outages, vehicles with stale GPS, trips cancelled without a
cancellation flag, realtime trip ids that don't match the schedule. See the sections below.
""")

# ---- coverage & quality ----------------------------------------------------------------------
with card():
    st.subheader("How much of the service we measured")
    if not marts_ready():
        st.info("Coverage tables appear once the analysis has run; it refreshes every 15 minutes.")
    else:
        cov = q("select * from marts.mart_daily_coverage order by service_date")
        if cov.empty:
            st.info("No coverage rows yet.")
        elif "trips_due" not in cov.columns:  # in the minutes after an update, before the rebuild
            st.info("Coverage is being recalculated; it reappears within 15 minutes.")
        else:
            cov["Date"] = cov["service_date"].map(fmt_date)
            for c in ("trips_due", "trips_reported", "mid_stops_due", "mid_stops_timed"):
                cov[c] = pd.to_numeric(cov[c]).fillna(0)
            cov["Stops we timed"] = cov["mid_stops_timed"] / cov["mid_stops_due"].where(
                cov["mid_stops_due"] > 0
            )
            cov["missing"] = (cov["trips_due"] - cov["trips_reported"]).clip(lower=0)
            # today is still in progress: its measures move all day, so charts and the 7-day
            # numbers use finished days only
            done = cov[pd.to_datetime(cov["service_date"]).dt.date < local_today()]
            week = done.tail(7)
            c1, c2 = st.columns(2)
            c1.metric(
                "Stops we timed, last 7 days",
                fmt_pct(int(week["mid_stops_timed"].sum()), int(week["mid_stops_due"].sum())),
                help="Of the stops that buses in LTD's live feed were due to pass, the share whose "
                "arrival time we measured (each trip's first and last stop left out: see below).",
            )
            c2.metric(
                "Bus arrivals measured, all days", f"{int(cov['stop_events_observed'].sum()):,}"
            )
            fig = go.Figure()
            fig.add_trace(
                go.Scatter(
                    x=done["Date"],
                    y=done["Stops we timed"],
                    mode="lines+markers",
                    name="Stops we timed",
                    line={"color": "#1f5f9e", "width": LINE_MAIN},
                    marker={"size": MARKER_MAIN},
                    hovertemplate="%{x}: %{y:.1%} of stops timed<extra></extra>",
                )
            )
            # the measure sits in the high 90s: the axis spans the data (at most 95% to 100%,
            # wider only if a day dips lower), so a one-point drop is visible
            low = min(0.95, math.floor((float(done["Stops we timed"].min()) - 0.005) * 100) / 100)
            fig.update_layout(
                yaxis={
                    "tickformat": ".0%",
                    "range": [low, 1.003],
                    "dtick": 0.01 if low >= 0.9 else None,
                    "title": "Stops we timed",
                },
                xaxis={"type": "category", "title": ""},
                showlegend=False,
                height=320,
            )
            show_chart(fig)
            st.caption(
                "Of the stops due on trips that ran, how many we timed; it should sit near 100%. "
                "A dip means our measurement missed stops: gaps between a bus's GPS reports, "
                "detours, or a trip that started reporting part-way along. Each trip's first and "
                "last stop are left out: at the first the bus is already sitting there when it "
                "starts reporting the trip, and at the last it often switches to its next trip "
                "first. Finished days only; today is in the table below."
            )

            st.markdown("##### Scheduled trips that never appeared in LTD's live feed")
            fig2 = go.Figure(
                go.Bar(
                    x=done["Date"],
                    y=done["missing"],
                    marker={"color": "#8a8f94"},
                    customdata=list(zip(done["trips_due"], strict=False)),
                    hovertemplate="%{x}: %{y} of %{customdata[0]:,} scheduled trips never "
                    "appeared<extra></extra>",
                )
            )
            fig2.update_layout(
                xaxis={"type": "category", "title": ""},
                yaxis={"title": "Trips"},
                showlegend=False,
                height=260,
            )
            show_chart(fig2)
            st.caption(
                "In LTD's timetable, but never in its live feed: usually a cancelled trip, "
                "sometimes a bus whose tracker was off (the feed doesn't say which). These trips "
                "have nothing to measure, so they aren't counted anywhere else on the site. A "
                "gap in our own collection would show here too: see Feed gaps in the table."
            )
            # every day's counts, for anyone checking: folded away by default
            day_by_day = st.expander("Day by day, in numbers")
            day_by_day.dataframe(
                arrow_safe(
                    pd.DataFrame(
                        {
                            "Date": pd.to_datetime(cov["service_date"]).dt.date,
                            "Buses seen": cov["vehicles_reporting"],
                            "Trips scheduled": cov["trips_scheduled"],
                            "Trips in live feed": cov["trips_seen"],
                            "Mid-trip stops due": cov["mid_stops_due"],
                            "Timed": cov["mid_stops_timed"],
                            "Stop events scheduled": cov["stop_events_scheduled"],
                            "Observed": cov["stop_events_observed"],
                            "Implausible": cov["stop_events_implausible"],
                            "Doubtful stop": cov["stop_events_doubtful"],
                            "Imprecise": cov["stop_events_imprecise"],
                            "Feed gaps": pd.to_numeric(cov["gap_minutes"]).fillna(0).astype(float),
                        }
                    )
                ),
                hide_index=True,
                width="stretch",
                column_config={
                    "Date": col_date(
                        "Date",
                        help="Service day (trips after midnight count with the day they started).",
                    ),
                    "Buses seen": col_count(
                        "Buses seen",
                        help="Different buses (vehicles) that reported a position that day.",
                    ),
                    "Trips scheduled": col_count(
                        "Trips scheduled", help="Trips in LTD's timetable for that day."
                    ),
                    "Trips in live feed": col_count(
                        "Trips in live feed",
                        help="Scheduled trips that appeared in LTD's live feed at all.",
                    ),
                    "Mid-trip stops due": col_count(
                        "Mid-trip stops due",
                        help="Stops those trips were due to pass, leaving out each trip's first "
                        "and last stop.",
                    ),
                    "Timed": col_count(
                        "Timed", help="Of those stops, the ones whose arrival time we measured."
                    ),
                    "Stop events scheduled": col_count(
                        "Stop events scheduled",
                        help="Every stop of every scheduled trip that day, first and last "
                        "included.",
                    ),
                    "Observed": col_count(
                        "Observed",
                        help="Arrivals measured and used on the site (after leaving out the "
                        "ones left out, in the next three columns).",
                    ),
                    "Implausible": col_count(
                        "Implausible",
                        help="Arrivals left out because they were more than 30 min early or 90 "
                        "min late against the timetable: in practice a bus reporting the wrong "
                        "trip, not real service.",
                    ),
                    "Doubtful stop": col_count(
                        "Doubtful stop",
                        help="Arrivals left out because they were at a stop where, as a rule, "
                        "our GPS time is more than 2 minutes out of line with the stops just "
                        "before and after it (checked over the last 30 days): our method places "
                        "the bus wrongly there.",
                    ),
                    "Imprecise": col_count(
                        "Imprecise",
                        help="Arrivals left out because the bus's reports around that stop were "
                        "more than 4 minutes apart (usually a feed outage), so the time can't be "
                        "pinned down to within 2 minutes.",
                    ),
                    "Feed gaps": col_minutes(
                        "Feed gaps",
                        help="Minutes when our fetches of LTD's feed were more than 2 minutes "
                        "apart.",
                    ),
                },
            )
            day_by_day.caption("Hover a column heading for what it counts.")

# ---- cross-check ------------------------------------------------------------------------------
with card():
    st.subheader("Two ways of timing arrivals, compared")
    st.caption(
        "Most arrival times on this site come from the bus's GPS reports: the moment its "
        "position along the route passes the stop. LTD's feed keeps its own time for each stop "
        "a bus has passed, but that is a real measurement only at timepoints (the few main stops "
        "on each route with an exact time in the timetable, explained above). At every other "
        "stop LTD's time is just its last estimate. So the two are compared at timepoints, "
        "where both really measured the bus."
    )
    if not marts_ready():
        st.caption("Appears once the analysis has run.")
    else:
        agree = q("""
            select sum(n_both_timepoints) as n_tp, sum(n_within_2min_timepoints) as w_tp,
                   percentile_cont(0.5) within group (order by median_abs_diff_timepoints_s) as med_tp,
                   percentile_cont(0.5) within group (order by median_diff_s) as med_all
            from marts.mart_method_agreement
        """).iloc[0]
        if agree["n_tp"] and int(agree["n_tp"]) > 0:
            c1, c2, c3 = st.columns(3)
            c1.metric(
                "Agree within 2 min at timepoints",
                fmt_pct(int(agree["w_tp"]), int(agree["n_tp"])),
                help="Our GPS-derived time and LTD's recorded time for the same bus at the same "
                "timepoint, within 2 minutes of each other (the most an arrival time on this site "
                "may be uncertain by).",
            )
            c2.metric(
                "Typical gap at timepoints",
                f"{float(agree['med_tp']):.0f} s",
                help="The median difference between the two, either way. Each is only known to "
                "within about 15 seconds (buses report every 30 s).",
            )
            c3.metric(
                "LTD's time between timepoints",
                f"{abs(float(agree['med_all'])):.0f} s earlier",
                help="Typically, at the stops between timepoints, where LTD's time is a "
                "estimate rather than a measurement. Our GPS times at those stops line up with "
                "our GPS times at the timepoints either side, so the gap is in LTD's estimates "
                "and no correction is made for it.",
            )
            # how far apart the two are, timepoints against the other stops (last 30 days)
            diffs = q(
                """
                select is_timepoint,
                       greatest(-300, least(300, (floor(methods_diff_s / 15.0) * 15)::int)) as bin,
                       count(*) as n
                from marts.fct_stop_events
                where methods_diff_s is not null and is_bounded and service_date >= current_date - 30
                group by 1, 2
                """
            )
            if not diffs.empty:
                fig = go.Figure()
                for tp, name, colour in (
                    (True, "Timepoints (LTD measures)", "#1f5f9e"),
                    (False, "Other stops (LTD predicts)", "#9aa0a6"),
                ):
                    d = diffs[diffs["is_timepoint"] == tp].sort_values("bin")
                    share = d["n"] / d["n"].sum()
                    fig.add_trace(
                        go.Bar(
                            x=(d["bin"] + 7.5) / 60,
                            y=share,
                            name=name,
                            marker={"color": colour},
                            opacity=0.85,
                            hovertemplate=name
                            + ": %{y:.1%} of arrivals %{x:.2f} min apart<extra></extra>",
                        )
                    )
                fig.update_layout(
                    barmode="overlay",
                    bargap=0.05,
                    xaxis={
                        "title": "LTD's time minus ours (minutes; below 0 = LTD's is earlier)",
                        "range": [-5, 5],
                        "dtick": 1,
                    },
                    yaxis={"title": "Share of arrivals", "tickformat": ".0%"},
                    legend={"orientation": "h", "yanchor": "bottom", "y": 1.02, "x": 0},
                    height=340,
                )
                show_chart(fig)

# ---- questions and answers (the same as the plain page /reports/faq/, for search engines) ----
with card():
    st.subheader("Questions and answers")
    for question, answer in questions():
        with st.expander(question):
            st.markdown(answer)

# ---- behind the scenes: last, for the few who want the raw workings ----------------------
with card():
    st.subheader(
        "Behind the scenes",
        help="The Arrivals board shows the live feed processed bus by bus; Status shows whether "
        "data is flowing and when the analysis last ran.",
    )
    b1, b2 = st.columns(2)
    b1.page_link("views/arrivals.py", label="Arrivals board →")
    b2.page_link("views/status.py", label="Status →")
