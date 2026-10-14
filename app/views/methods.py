"""Data & methods: how the site measures lateness, how much of the service it measured, and the
health of LTD's feed right now."""

from __future__ import annotations

import math

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from common import (
    BRAND,
    LINE_COLOURS,
    LINE_MAIN,
    LOCAL_TZ,
    MARKER_MAIN,
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
    table,
)

require_db()
st.title("Data & methods")
st.caption(
    "How the numbers on this site are made, and how complete they are. Behind the scenes: the "
    "Arrivals board shows the live feed processed bus by bus, and Status shows whether data is "
    "flowing and when the analysis last ran."
)
b1, b2 = st.columns(2)
b1.page_link("views/arrivals.py", label="Arrivals board →")
b2.page_link("views/status.py", label="Status →")

# ---- definitions -------------------------------------------------------------------------
with card():
    st.subheader("How it's measured")
    start = collection_start()
    st.markdown(f"""
**Sources.** Lane Transit District's public GTFS schedule and GTFS-Realtime feeds (TripUpdates, VehiclePositions, Alerts),
polled every 30 seconds since {fmt_date(start) if start else "—"}. LTD is the source of all schedule and vehicle data;
this project is independent and not affiliated with LTD.

**Lateness.** For every bus at every stop: the time it actually arrived minus the time the printed timetable gives for
that stop. Positive is late, negative is early. Every stop counts, not only the timepoints the schedule guarantees,
because riders wait at every stop; between timepoints the timetable's times are interpolated by LTD, and our check
shows lateness grows smoothly between them (a typical extra 2 seconds), so the minor stops don't distort the picture.

**Typical bus.** The median lateness: half the buses were later than this, half earlier.

**8 in 10 buses.** The range from the 10th to the 90th percentile of lateness: one bus in ten came earlier than the
low end and one in ten later than the high end. Its width is how much you need to allow for when planning.

**Minutes off.** How far the bus was from the timetable, early or late alike, averaged: a route whose buses are
2 minutes early half the time and 2 minutes late the rest is 2 minutes off, though its typical bus is on time.

**Today so far.** The typical bus over today's service, as of the latest update (every 15 minutes).

**Prediction accuracy.** Every real-time arrival prediction LTD publishes is kept, with when it was shown.
"5 minutes away" means the prediction said 5:00 to 5:59. Each bus arrival counts once at each distance (the
prediction on show at that moment), and the timetable is measured on the same arrivals. Minutes off = how far the
actual arrival was from the predicted one, early or late alike; the charts average it.

**Observed arrival.** LTD's vehicle positions carry no stop information, so arrivals are derived geometrically: each
position is placed along the route's shape as a fraction of the way along it (walked in time order, each position on
the first stretch of the route ahead of the last, so a route that uses a street twice can't be confused), the running
maximum over time removes GPS jitter, and when that fraction steps past a stop between two reports the crossing time is
interpolated (±15 s at a 30 s poll while moving). Independently, the feed keeps reporting a departure time for stops
already passed that no longer changes; that "settled" time is recorded too, and the two are compared in the cross-check
below. Stops passed before a trip's first report can't be timed and are excluded. What counts is when the bus leaves: at
a trip's first stop it usually arrives early and waits, and at a timepoint an early bus holds until its time, so at
both LTD's recorded departure is used (a real measurement there). Everywhere else our GPS time is used, which in practice
is when the bus goes past or pulls away.

**Headways.** On routes scheduled every 20 minutes or better, the gap between consecutive buses at a stop is compared to
the scheduled gap. Bunched = under half the scheduled gap; big gap = over 1.5×.

**Excluded.** Trips with no realtime data; stops with no bounded observed arrival; arrivals more than 30 minutes early or
90 minutes late against the schedule, which in practice means a bus reporting the wrong trip rather than real service
(counted as "Implausible" in the coverage table below); arrivals whose time can't be pinned down to within 2 minutes
because the bus's reports around that stop were far apart, usually during a feed outage ("Imprecise" below);
stops between timepoints where our GPS time and LTD's own record are more than 2 minutes apart as a rule, usually a
stop whose mapped position isn't where buses really stop ("Doubtful stop" below);
predictions more than 90 minutes out or made after the bus had arrived; the first stop of a trip for headway purposes.

**Known failure modes we measure rather than assume.** Feed outages, vehicles with stale GPS, trips cancelled without a
cancellation flag, realtime trip ids that don't match the schedule. See the sections below.
""")

# ---- coverage & quality ----------------------------------------------------------------------
with card():
    st.subheader("How much of the service did we measure?")
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
            table(
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
                        help="Arrivals left out because they were at a stop where our GPS time "
                        "and LTD's own record are more than 2 minutes apart as a rule (checked "
                        "over the last 30 days), so neither time can be trusted there.",
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
            st.caption("Hover a column heading for what it counts.")

# ---- cross-check ------------------------------------------------------------------------------
with card():
    st.subheader("Do two ways of timing arrivals agree?")
    st.caption(
        "Every arrival time on this site is worked out from the bus's GPS reports: when its "
        "position along the route passes the stop. LTD's feed also keeps a time for stops a bus "
        "has passed, but LTD only records actual times at timepoints (the stops its timetable "
        "is built around); between them its time is its last prediction, not an observation. "
        "So the two are compared at timepoints, where both are real measurements."
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
                "prediction rather than a measurement. The gap is LTD's: our times at those "
                "stops line up with the timepoints either side, where the two agree, so no "
                "correction is made for it.",
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
            per_route = q(
                """
                select route_short_name as route, sum(n_both_timepoints) as n,
                       sum(n_within_2min_timepoints)::float8 / nullif(sum(n_both_timepoints), 0)
                           as share
                from marts.mart_method_agreement
                group by 1 having sum(n_both_timepoints) >= 50
                order by share
                """
            )
            if not per_route.empty:
                st.markdown("##### Agreement at timepoints, route by route")
                fig_r = go.Figure(
                    go.Bar(
                        y=per_route["route"].astype(str),
                        x=per_route["share"],
                        orientation="h",
                        marker={"color": "#1f5f9e"},
                        customdata=per_route["n"],
                        hovertemplate="Route %{y}: %{x:.0%} within 2 min, of %{customdata:,} "
                        "comparisons<extra></extra>",
                    )
                )
                fig_r.update_layout(
                    xaxis={
                        "title": "Within 2 minutes of LTD's time",
                        "tickformat": ".0%",
                        "range": [0, 1],
                    },
                    yaxis={"type": "category", "title": ""},
                    height=max(260, 22 * len(per_route) + 80),
                    showlegend=False,
                )
                show_chart(fig_r)
                st.caption(
                    "Routes near the top are where our GPS method has the most trouble, usually "
                    "long rural routes where the bus covers a lot of road between reports."
                )

GAP_DAYS = 14

# ---- right now --------------------------------------------------------------------------------
with card():
    st.subheader("Is the live data coming in?")
    qual = q("""
        select
          (select count(distinct p.trip_id) from rt.prediction_current p
            where p.start_date >= current_date - 1
              and not exists (select 1 from gtfs.trips t where t.trip_id = p.trip_id)) as unmatched_trips,
          (select count(*) from (
              select distinct on (vehicle_id) vehicle_id, position_timestamp, fetch_id
              from rt.vehicle_position where position_timestamp > now() - interval '1 hour'
              order by vehicle_id, position_timestamp desc) v
            join rt.fetch f on f.fetch_id = v.fetch_id
            where f.fetched_at > now() - interval '10 minutes'
              and v.position_timestamp < f.header_timestamp - interval '3 minutes') as stale_gps,
          (select count(*) from rt.fetch where fetched_at > now() - interval '24 hours') as fetches_24h
    """)
    r = qual.iloc[0]
    c1, c2, c3 = st.columns(3)
    c1.metric(
        "Live trips not in the timetable",
        int(r["unmatched_trips"]),
        help="Trips in LTD's live feed (today and yesterday) that aren't in its published "
        "timetable: usually a few added or replacement trips. Dozens would mean the timetable "
        "is out of date.",
    )
    c2.metric(
        "Buses with stale GPS now",
        int(r["stale_gps"]),
        help="Reporting a position more than 3 minutes older than the feed itself.",
    )
    c3.metric(
        "Messages received, last 24 h",
        f"{int(r['fetches_24h']):,}",
        help="About 6,000 when everything runs: bus positions and predictions twice a minute "
        "each, plus alerts every 5 minutes.",
    )
    # Minutes without live data, per day: a steady feed is two messages a minute, so a flat
    # message count says nothing; what matters is when there was none, and whose doing that was.
    TODAY = f"(now() at time zone '{LOCAL_TZ}')::date"  # Eugene's date, not the server's
    gaps = q(
        f"""
        with days as (
            select d::date as day
            from generate_series({TODAY} - {GAP_DAYS}, {TODAY} - 1, interval '1 day') d
        ),
        got as (
            select (fetched_at at time zone '{LOCAL_TZ}')::date as day, count(*) as n
            from rt.fetch
            where feed = 'vehicle_positions'
              and fetched_at >= ({TODAY} - {GAP_DAYS})::timestamp at time zone '{LOCAL_TZ}'
              and fetched_at < {TODAY}::timestamp at time zone '{LOCAL_TZ}'
            group by 1
        ),
        service as (
            select service_date as day, min(scheduled_arrival) as s0, max(scheduled_arrival) as s1
            from marts.fct_stop_events
            where service_date >= {TODAY} - {GAP_DAYS} and service_date < {TODAY}
            group by 1
        ),
        empty as (
            select s.day, count(*) as n
            from service s
            join rt.fetch f on f.feed = 'vehicle_positions' and f.entity_count = 0
             and f.fetched_at between s.s0 and s.s1
            group by 1
        )
        select d.day,
               -- two a minute through the day (a clock-change day is 23 or 25 hours long)
               greatest(0, extract(epoch from ((d.day + 1)::timestamp at time zone '{LOCAL_TZ}')
                                   - (d.day::timestamp at time zone '{LOCAL_TZ}')) / 30
                           - coalesce(g.n, 0)) / 2.0 as missed_min,
               coalesce(e.n, 0) / 2.0 as empty_min
        from days d left join got g using (day) left join empty e using (day)
        -- whole days since collection began
        where d.day > (select min(fetched_at at time zone '{LOCAL_TZ}')::date from rt.fetch)
        order by 1
        """
    )
    st.markdown(
        f"##### Minutes without live data, last {GAP_DAYS} days",
        help="LTD's feed is read every 30 seconds, day and night. Not collected: minutes when "
        "this site's collector got nothing (a server restart, or LTD's servers not answering). "
        "LTD's feed empty: minutes while buses were scheduled when LTD's feed answered but had "
        "no buses in it. Arrivals in those minutes can't be timed, so they're left out.",
    )
    if gaps.empty or (gaps["missed_min"].sum() + gaps["empty_min"].sum()) < 1:
        st.caption(f"None in the last {GAP_DAYS} days.")
    else:
        fig_g = go.Figure()
        for col, name, colour in (
            ("missed_min", "Not collected", BRAND),
            ("empty_min", "LTD's feed empty", LINE_COLOURS[0]),
        ):
            fig_g.add_trace(
                go.Bar(
                    x=pd.to_datetime(gaps["day"]),
                    y=gaps[col],
                    name=name,
                    marker_color=colour,
                    hovertemplate=name + ", %{x|%a %b %-d}: %{y:.0f} min<extra></extra>",
                )
            )
        fig_g.update_layout(
            barmode="stack",
            yaxis={"title": "Minutes", "rangemode": "tozero"},
            xaxis={"title": "", "tickformat": "%a %b %-d", "dtick": 86400000},
            legend={
                "orientation": "h",
                "yanchor": "bottom",
                "y": 1.02,
                "x": 0,
                "traceorder": "normal",
            },
            height=300,
        )
        show_chart(fig_g)
