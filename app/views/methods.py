"""Data & methods: how the site measures lateness, how much of the service it measured, and the
health of LTD's feed right now."""

from __future__ import annotations

import math

import pandas as pd
import plotly.express as px
import streamlit as st
from common import (
    col_count,
    col_date,
    col_minutes,
    collection_start,
    fit_phone,
    fmt_date,
    fmt_pct,
    local_today,
    marts_ready,
    q,
    require_db,
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
with st.container(border=True):
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

**Today so far.** The typical bus over today's service, as of the latest update (every 15 minutes).

**Countdown accuracy.** Every predicted arrival LTD's countdown publishes is kept, with when it was displayed.
"5 minutes out" means the countdown showed 5:00 to 5:59. Error = actual arrival − the arrival the countdown promised,
so positive means the bus came later than the countdown said.

**Observed arrival.** LTD's vehicle positions carry no stop information, so arrivals are derived geometrically: each
position is placed along the route's shape as a fraction of the way along it (walked in time order, each position on
the first stretch of the route ahead of the last, so a route that uses a street twice can't be confused), the running
maximum over time removes GPS jitter, and when that fraction steps past a stop between two reports the crossing time is
interpolated (±15 s at a 30 s poll while moving). Independently, the feed keeps reporting a departure time for stops
already passed that no longer changes; that "settled" time is recorded too, and the two are compared in the cross-check
below. Stops passed before a trip's first report can't be timed and are excluded.

**Headways.** On routes scheduled every 20 minutes or better, the gap between consecutive buses at a stop is compared to
the scheduled gap. Bunched = under half the scheduled gap; big gap = over 1.5×.

**Excluded.** Trips with no realtime data; stops with no bounded observed arrival; arrivals more than 30 minutes early or
90 minutes late against the schedule, which in practice means a bus reporting the wrong trip rather than real service
(counted as "Implausible" in the coverage table below); arrivals whose time can't be pinned down to within 2 minutes
because the bus's reports around that stop were far apart, usually during a feed outage ("Imprecise" below);
predictions more than 90 minutes out or made after the bus had arrived; the first stop of a trip for headway purposes.

**Known failure modes we measure rather than assume.** Feed outages, vehicles with stale GPS, trips cancelled without a
cancellation flag, realtime trip ids that don't match the schedule. See the sections below.
""")

# ---- coverage & quality ----------------------------------------------------------------------
with st.container(border=True):
    st.subheader("Coverage: how much of the service we measured")
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
            cov["Trips LTD reported"] = cov["trips_reported"] / cov["trips_due"].where(
                cov["trips_due"] > 0
            )
            cov["Stops we timed on them"] = cov["mid_stops_timed"] / cov["mid_stops_due"].where(
                cov["mid_stops_due"] > 0
            )
            # today is still in progress: its measures move all day, so charts and the 7-day
            # numbers use finished days only
            done = cov[pd.to_datetime(cov["service_date"]).dt.date < local_today()]
            week = done.tail(7)
            c1, c2, c3 = st.columns(3)
            c1.metric(
                "Trips LTD reported, last 7 days",
                fmt_pct(int(week["trips_reported"].sum()), int(week["trips_due"].sum())),
                help="Scheduled trips that appeared in LTD's realtime feed at all.",
            )
            c2.metric(
                "Stops we timed on them, last 7 days",
                fmt_pct(int(week["mid_stops_timed"].sum()), int(week["mid_stops_due"].sum())),
                help="On those trips, the stops whose arrival we measured, leaving out each "
                "trip's first and last stop.",
            )
            c3.metric("Stop events scored, all days", f"{int(cov['stop_events_observed'].sum()):,}")
            long = done.melt(
                id_vars=["Date"],
                value_vars=["Trips LTD reported", "Stops we timed on them"],
                var_name="Measure",
                value_name="Share",
            )
            fig = px.line(
                long,
                x="Date",
                y="Share",
                color="Measure",
                markers=True,
                color_discrete_sequence=["#1f5f9e", "#e07b00"],
            )
            # both measures sit in the high 90s: the axis spans the data (at most 95% to 100%,
            # wider only if a day dips lower), so a one-point drop is visible
            low = min(0.95, math.floor((float(long["Share"].min()) - 0.005) * 100) / 100)
            fig.update_layout(
                yaxis_tickformat=".0%",
                yaxis_range=[low, 1.003],
                yaxis_dtick=0.01 if low >= 0.9 else None,
                yaxis_title="",
                xaxis_title="",
                legend={
                    "orientation": "h",
                    "yanchor": "bottom",
                    "y": 1.02,
                    "x": 0,
                    "xanchor": "left",
                },
                legend_title="",
            )
            st.plotly_chart(fit_phone(fig), width="stretch")
            st.caption(
                "Both should sit near 100%. A dip in **trips LTD reported** is on LTD's side (a "
                "cancelled trip, or a bus whose tracker was off) or a gap in our own collection: "
                "check Feed gaps in the table. A dip in **stops we timed** means our measurement "
                "missed stops on trips that did report: gaps between a bus's GPS reports, detours, "
                "or a trip that started reporting part-way along. Each trip's first and last stop "
                "are left out of that measure because the method can't time them reliably: at the "
                "first stop the bus is already sitting there when it starts reporting the trip, "
                "and at the last it often switches to its next trip first. Finished days only; "
                "today is in the table below."
            )
            table(
                pd.DataFrame(
                    {
                        "Date": pd.to_datetime(cov["service_date"]).dt.date,
                        "Buses seen": cov["vehicles_reporting"],
                        "Trips scheduled": cov["trips_scheduled"],
                        "Trips seen": cov["trips_seen"],
                        "Mid-trip stops due": cov["mid_stops_due"],
                        "Timed": cov["mid_stops_timed"],
                        "Stop events scheduled": cov["stop_events_scheduled"],
                        "Observed": cov["stop_events_observed"],
                        "Implausible": cov["stop_events_implausible"],
                        "Imprecise": cov["stop_events_imprecise"],
                        "Feed gaps": pd.to_numeric(cov["gap_minutes"]).fillna(0).astype(float),
                    }
                ),
                hide_index=True,
                width="stretch",
                column_config={
                    "Date": col_date("Date"),
                    "Buses seen": col_count("Buses seen"),
                    "Trips scheduled": col_count("Trips scheduled"),
                    "Trips seen": col_count("Trips seen"),
                    "Mid-trip stops due": col_count(
                        "Mid-trip stops due",
                        help="On reported trips, excluding first and last stops.",
                    ),
                    "Timed": col_count("Timed"),
                    "Stop events scheduled": col_count("Stop events scheduled"),
                    "Observed": col_count("Observed"),
                    "Implausible": col_count("Implausible"),
                    "Imprecise": col_count("Imprecise"),
                    "Feed gaps": col_minutes("Feed gaps"),
                },
            )
            st.caption(
                "Feed gaps count minutes where consecutive fetches were more than 2 minutes apart. "
                "Implausible = arrivals left out because they were more than 30 min early or 90 min late (a bus reporting the wrong trip). "
                "Imprecise = arrivals left out because the bus's reports around that stop were more than 4 minutes apart "
                "(usually a feed outage), so the time can't be pinned down to within 2 minutes."
            )

# ---- cross-check ------------------------------------------------------------------------------
with st.container(border=True):
    st.subheader("Cross-check: do two ways of timing arrivals agree?")
    if not marts_ready():
        st.caption("Appears once the analysis has run.")
    else:
        agree = q("""
            select sum(n_both) as n_all, sum(n_within_1min) as w_all,
                   sum(n_both_timepoints) as n_tp, sum(n_within_1min_timepoints) as w_tp,
                   percentile_cont(0.5) within group (order by median_abs_diff_timepoints_s) as med_tp,
                   percentile_cont(0.5) within group (order by median_diff_s) as med_all
            from marts.mart_method_agreement
        """).iloc[0]
        if agree["n_tp"] and int(agree["n_tp"]) > 0:
            c1, c2, c3 = st.columns(3)
            c1.metric(
                "Agree within 1 min at timepoints",
                fmt_pct(int(agree["w_tp"]), int(agree["n_tp"])),
                help="Position-derived arrival vs the feed's own settled time, at stops the schedule guarantees.",
            )
            c2.metric("Typical gap at timepoints", f"{float(agree['med_tp']):.0f} s")
            c3.metric(
                "Feed vs geometry, all stops",
                f"{float(agree['med_all']):+.0f} s",
                help="Negative: the feed's time is earlier. Between timepoints the feed reports its last prediction, not an observation.",
            )
            st.caption(
                "The geometric method is the reference for every stop; the feed's settled times are the cross-check where LTD records actuals (timepoints)."
            )

# ---- right now --------------------------------------------------------------------------------
with st.container(border=True):
    st.subheader("Feed health right now")
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
        "Realtime trips not in schedule",
        int(r["unmatched_trips"]),
        help="Should be 0; non-zero means the schedule is out of date.",
    )
    c2.metric(
        "Buses with stale GPS",
        int(r["stale_gps"]),
        help="Reporting a position more than 3 minutes older than the feed itself.",
    )
    c3.metric(
        "Fetches, last 24 h",
        f"{int(r['fetches_24h']):,}",
        help="About 6,000 when everything runs (2 feeds × 2 per minute, plus alerts every 5 minutes); fewer when the feed didn't change between polls.",
    )
