"""Data & methods: definitions, coverage and quality, the data explorer, downloads."""

from __future__ import annotations

import pandas as pd
import plotly.express as px
import streamlit as st
from common import (
    EXPLORER_MAX_ROWS,
    arrow_safe,
    col_count,
    col_date,
    col_minutes,
    collection_start,
    current_fv,
    download_button,
    fit_phone,
    fmt_date,
    fmt_pct,
    marts_ready,
    q,
    require_db,
    run_explorer_query,
    table,
)

require_db()
st.title("Data & methods")
st.caption("Behind the scenes, for anyone curious how the data flows:")
b1, b2 = st.columns(2)
b1.page_link(
    "views/arrivals.py", label="Arrivals board: the live feed, processed, bus by bus", icon="🕒"
)
b2.page_link(
    "views/status.py",
    label="Status: is data flowing, and when did the analysis last run?",
    icon="🔧",
)

tab_defs, tab_cov, tab_explore, tab_dl = st.tabs(
    ["Definitions", "Coverage & quality", "Data explorer", "Downloads"]
)

# ---- definitions -------------------------------------------------------------------------
with tab_defs:
    start = collection_start()
    st.markdown(f"""
**Sources.** Lane Transit District's public GTFS schedule and GTFS-Realtime feeds (TripUpdates, VehiclePositions, Alerts),
polled every 30 seconds since {fmt_date(start) if start else "—"}. LTD is the source of all schedule and vehicle data;
this project is independent and not affiliated with LTD.

**On time.** An arrival at a *timepoint* (a stop the schedule guarantees a time for) no more than 1 minute early and
no more than 5 minutes late — the convention used by most US agencies (TCRP). "Early" and "late" are outside that window.
Stop-level pages use all stops, not just timepoints, because riders wait at every stop.

**Observed arrival.** LTD's vehicle positions carry no stop information, so arrivals are derived geometrically: each
position is placed along the route's shape as a fraction of the way along it (walked in time order, each position on
the first stretch of the route ahead of the last, so a route that uses a street twice can't be confused), the running
maximum over time removes GPS jitter, and when that fraction steps past a stop between two reports the crossing time is interpolated (±15 s at a
30 s poll while moving). Independently, the feed keeps reporting a departure time for stops already passed that no longer
changes; that "settled" time is recorded too, and the two are compared on the Coverage & quality tab. Stops passed before
a trip's first report can't be timed and are excluded.

**Prediction horizon and error.** Every predicted arrival the feed publishes is kept, with the interval it was displayed.
Horizon = predicted arrival − the moment the prediction first appeared. Error = observed arrival − predicted arrival,
so positive means the bus came later than the sign said.

**Headways.** On routes scheduled every 20 minutes or better, the gap between consecutive buses at a stop is compared to
the scheduled gap. Bunched = under half the scheduled gap; big gap = over 1.5×.

**Excluded.** Trips with no realtime data; stops with no bounded observed arrival; arrivals more than 30 minutes early or
90 minutes late against the schedule, which in practice means a bus reporting the wrong trip rather than real service
(they are counted on the Coverage & quality tab); arrivals whose time can't be pinned down to within 2 minutes because
the bus's reports around that stop were far apart, usually during a feed outage (also counted there); predictions more
than 90 minutes out or made after the bus had arrived;
the first stop of a trip for headway purposes.

**Known failure modes we measure rather than assume.** Feed outages, vehicles with stale GPS, trips cancelled without a
cancellation flag, realtime trip ids that don't match the schedule. See the Coverage & quality tab.
""")

# ---- coverage & quality ----------------------------------------------------------------------
with tab_cov:
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
            week = cov.tail(7)
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
            long = cov.melt(
                id_vars=["Date"],
                value_vars=["Trips LTD reported", "Stops we timed on them"],
                var_name="Measure",
                value_name="Share",
            )
            fig = px.line(long, x="Date", y="Share", color="Measure", markers=True)
            fig.update_layout(
                yaxis_tickformat=".0%",
                yaxis_range=[0, 1.02],
                yaxis_title="",
                xaxis_title="",
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
                "and at the last it often switches to its next trip first. Stops due in the last "
                "two hours are not counted yet."
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

    if marts_ready():
        st.markdown("**Do the two arrival methods agree?**")
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

    st.markdown("**Right now**")
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
        int(r["fetches_24h"]),
        help="About 6,000 when everything runs (2 feeds × 2 per minute, plus alerts every 5 minutes); fewer when the feed didn't change between polls.",
    )

# ---- data explorer ----------------------------------------------------------------------------
with tab_explore:
    st.caption(
        "Every table in the warehouse, read-only. Raw tables (gtfs, rt), the analysis layer (staging, intermediate, marts)."
    )
    tables = q("""
        select table_schema || '.' || table_name as tbl
        from information_schema.tables
        where table_schema in ('gtfs', 'rt', 'staging', 'intermediate', 'marts')
          and table_type in ('BASE TABLE', 'VIEW')
        order by 1
    """)
    choice = st.selectbox("Table", tables["tbl"])
    schema, name = choice.split(".", 1)
    cols = q(
        "select column_name, data_type from information_schema.columns where table_schema = %s and table_name = %s order by ordinal_position",
        (schema, name),
    )
    est = q(
        """
        select c.reltuples::bigint as n, c.relkind::text as kind
        from pg_class c join pg_namespace s on s.oid = c.relnamespace
        where s.nspname = %s and c.relname = %s
        """,
        (schema, name),
    )
    if est.empty or est["kind"][0] == "v":
        st.caption("A view: rows are computed when queried.")
    elif int(est["n"][0]) < 0:
        st.caption("Row count not estimated yet.")
    else:
        st.caption(f"About {int(est['n'][0]):,} rows")
    left, right = st.columns([1, 2])
    with left:
        table(cols, hide_index=True, width="stretch")
    with right:
        n = st.slider("Rows", 10, 500, 100)
        order = st.selectbox("Order by (desc)", ["(none)"] + list(cols["column_name"]))
        order_sql = "" if order == "(none)" else f' order by "{order}" desc'
        try:
            preview, _ = run_explorer_query(
                f'select * from "{schema}"."{name}"{order_sql} limit {int(n)}'
            )
            table(preview, hide_index=True, width="stretch")
        except Exception as exc:  # noqa: BLE001
            st.error(f"Could not read the table: {str(exc).splitlines()[0]}")

    st.markdown(
        f"**Run a query** (one SELECT, read-only, 15-second limit, first {EXPLORER_MAX_ROWS:,} rows)"
    )
    default = """select route_short_name, count(*) as scored, round(100.0 * count(*) filter (where status = 'on_time') / count(*)) as pct_on_time
from marts.fct_stop_events
where status is not null and is_timepoint
group by 1 order by 3 desc"""
    sql = st.text_area("SQL", default, height=160)
    if st.button("Run"):
        try:
            result, truncated = run_explorer_query(sql)
            st.caption(
                f"First {len(result):,} rows (more exist)" if truncated else f"{len(result):,} rows"
            )
            table(arrow_safe(result), hide_index=True, width="stretch")
            download_button(result, "query_result.csv")
        except Exception as exc:  # noqa: BLE001
            st.error(str(exc).strip().splitlines()[0])

# ---- downloads ---------------------------------------------------------------------------------
with tab_dl:
    st.markdown(
        "Everything here is derived from LTD's public feeds and is free to reuse with attribution to this project and to LTD."
    )
    if marts_ready():
        rc = q("""
            select route_short_name as route, sum(n_events) as timepoint_arrivals, sum(n_on_time) as on_time, sum(n_early) as early, sum(n_late) as late
            from marts.mart_route_daily group by 1 order by 1
        """)
        download_button(rc, "eugenebuswatch_route_totals_all_data.csv", "Route totals (all data)")
        if st.button("Prepare the scored stop events (latest 200,000)"):
            st.session_state["prepare_stop_events"] = True
        if st.session_state.get("prepare_stop_events"):
            se = q("""
                select service_date, route_short_name, direction_id, trip_id, stop_sequence, stop_id, is_timepoint,
                       scheduled_arrival, observed_arrival, delay_s, status
                from marts.fct_stop_events where status is not null order by service_date desc, trip_id, stop_sequence limit 200000
            """)
            download_button(
                se,
                "eugenebuswatch_stop_events_latest.csv",
                f"Scored stop events (latest {len(se):,} rows)",
            )
    else:
        st.info("Downloads appear once the analysis has run; it refreshes every 15 minutes.")
    stops = q(
        f"select stop_id, stop_code, stop_name, stop_lat, stop_lon from gtfs.stops where feed_version_id = {current_fv()} order by stop_name"
    )
    download_button(stops, "eugenebuswatch_stops.csv", "Stops (current schedule)")
