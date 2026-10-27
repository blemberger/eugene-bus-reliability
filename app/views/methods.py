"""Data & methods: how the site measures lateness, how much of the service it measured, and the
health of LTD's feed right now."""

from __future__ import annotations

import math
from datetime import timedelta

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
    current_fv,
    fmt_date,
    fmt_pct,
    lateness,
    local_today,
    marts_ready,
    q,
    require_db,
    show_chart,
)

from eugene_bus_reliability.faq import ANSWERED_ON_METHODS_PAGE, questions, stats_from_lateness

require_db()
st.title("Data & methods")
st.caption("How the numbers on this site are made, and how complete they are.")

# ---- definitions -------------------------------------------------------------------------
with card():
    st.subheader("How it's measured")
    start = collection_start()
    st.markdown(f"""
**Sources.** Lane Transit District's public data: its schedule, and its live feeds of bus positions, arrival
predictions and service alerts, read every 30 seconds since {fmt_date(start) if start else "—"}. This project is
independent and not affiliated with LTD.

**Timepoints.** On each route, a few main stops have an exact time in LTD's timetable; LTD calls them timepoints. A bus
that gets to one early is meant to wait there until its time. The times at the stops in between are LTD's estimates.

**Lateness.** For every bus at every stop: the time it actually got there minus the time the timetable gives for that
stop. Positive is late, negative is early.

**How arrival times are measured.** LTD's live data gives each bus's GPS position every 30 seconds, not when it reaches
a stop. So each position is placed along the bus's route, and when the bus passes a stop between two reports, the time
it passed is worked out from those two reports, to within about 15 seconds. At timepoints and at a trip's first stop,
where a bus may wait, LTD's own record of when the bus left is used as well: the later of the two times counts, since
either can catch the bus before it actually leaves. Our times are checked against LTD's records further down this page.

**Left out.** A measured arrival is left out when it is more than 30 minutes early or 90 minutes late (in practice a bus
reporting the wrong trip), when the bus's reports around the stop were too far apart to time it within 2 minutes, or
at the few stops where, trip after trip, our time is out of line with the stops either side (usually a stop mapped
away from where buses really stop). How many, day by day: "Day by day, in numbers", in the next section.

**Typical bus.** The median: half the buses were later than this, half earlier.

**8 in 10 buses.** The range from the 10th to the 90th percentile: one bus in ten came earlier than the low end, and
one in ten later than the high end.

**Minutes off.** How far the buses were from the timetable, early or late alike, on average: a route whose buses are
2 minutes early half the time and 2 minutes late the rest is 2 minutes off, though its typical bus is on time.

**Today so far.** The typical bus over today's service, as of the latest update (every 15 minutes).

**Prediction accuracy.** Every arrival prediction LTD publishes (what apps like Transit show) is saved. For each bus,
we take the prediction LTD was showing when it said the bus was 5 minutes away (5:00 to 5:59), and compare it with
when the bus actually came; the same at 1 to 15 minutes away. The printed timetable is scored on the same buses, for
comparison.
""")

# ---- coverage & quality ----------------------------------------------------------------------
COVERAGE_LEFT = 78  # room left of the two coverage charts for their axis labels and titles
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
            # the two charts below share their days: the same span (each day's line point over
            # the middle of its bar) and the same room for the numbers on the left, so a day in
            # one sits right above the same day in the other
            days_range = [-0.5, len(done) - 0.5]
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
                xaxis={"type": "category", "title": "", "range": days_range},
                showlegend=False,
                height=320,
            )
            show_chart(fig, left=COVERAGE_LEFT)
            st.caption(
                "Of the stops due on trips that ran, how many we timed; it should sit near 100%. "
                "A dip means we missed stops, usually through gaps in a bus's GPS reports or a "
                "detour. Each trip's first and last stops are left out of this count, as a bus "
                "often reports them before or after the trip itself. Finished days only; today "
                "is in the table below."
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
                xaxis={"type": "category", "title": "", "range": days_range},
                yaxis={"title": "Trips"},
                showlegend=False,
                height=260,
            )
            show_chart(fig2, left=COVERAGE_LEFT)
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
    st.subheader("Our arrival times checked against LTD's")
    st.caption(
        "At timepoints, LTD's feed records when each bus left, so there the same bus has two "
        "times: ours, worked out from its GPS reports, and LTD's. How closely they agree shows "
        "how well our method times the buses. Last 30 days."
    )
    if not marts_ready():
        st.caption("Appears once the analysis has run.")
    else:
        agree = q("""
            select sum(n_both_timepoints) as n_tp, sum(n_within_2min_timepoints) as w_tp,
                   percentile_cont(0.5) within group (order by median_abs_diff_timepoints_s) as med_tp
            from marts.mart_method_agreement
            where service_date >= current_date - 30
        """).iloc[0]
        if agree["n_tp"] and int(agree["n_tp"]) > 0:
            c1, c2 = st.columns(2)
            c1.metric(
                "Agree within 2 minutes",
                fmt_pct(int(agree["w_tp"]), int(agree["n_tp"])),
                help="Our time and LTD's for the same bus at the same timepoint, within 2 "
                "minutes of each other.",
            )
            c2.metric(
                "Typical difference",
                f"{float(agree['med_tp']):.0f} s",
                help="The median difference between the two, either way. Buses report their "
                "position every 30 seconds, so each time is only known to within about 15.",
            )
            diffs = q(
                """
                select greatest(-300, least(300, (floor(methods_diff_s / 15.0) * 15)::int)) as bin,
                       count(*) as n
                from marts.fct_stop_events
                where methods_diff_s is not null and is_bounded and is_timepoint
                  and service_date >= current_date - 30
                group by 1 order by 1
                """
            )
            if not diffs.empty:
                share = diffs["n"] / diffs["n"].sum()
                fig = go.Figure(
                    go.Bar(
                        x=(diffs["bin"] + 7.5) / 60,
                        y=share,
                        marker={"color": "#1f5f9e"},
                        hovertemplate="%{y:.1%} of buses: LTD's time %{x:.2f} min from ours"
                        "<extra></extra>",
                    )
                )
                fig.update_layout(
                    bargap=0.05,
                    xaxis={
                        "title": "LTD's time minus ours, in minutes (below 0: LTD's is earlier)",
                        "range": [-5, 5],
                        "dtick": 1,
                    },
                    yaxis={"title": "Share of buses", "tickformat": ".0%"},
                    showlegend=False,
                    height=320,
                )
                show_chart(fig)


# ---- questions and answers (the same as the plain page /reports/faq/, for search engines) ----
def _faq_numbers() -> dict:
    """The latest numbers the questions quote (last 30 days, all days), as on /reports/faq/."""
    start = local_today() - timedelta(days=29)
    overall = lateness(start, None, "overall")
    if overall.empty:
        return {}
    routes = lateness(start, None, "route")
    names = q(
        "select route_id, route_short_name as short, route_long_name as long from gtfs.routes "
        f"where feed_version_id = {current_fv()}"
    )
    label = {
        str(r.route_id): f"{r.short} ({r.long})" if r.long and r.long != r.short else str(r.short)
        for r in names.itertuples()
    }
    return stats_from_lateness(
        overall.to_dict("records")[0],
        routes.to_dict("records"),
        lambda rid: label.get(str(rid), str(rid)),
    )


with card():
    st.subheader("Questions and answers")
    for question, answer in questions(_faq_numbers()):
        if question in ANSWERED_ON_METHODS_PAGE:  # in "How it's measured" at the top
            continue
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
