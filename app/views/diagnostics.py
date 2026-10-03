"""Diagnostics: is data flowing, how much, and has the analysis layer been rebuilt?"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st
from common import (
    col_late,
    col_minutes,
    col_time,
    current_fv,
    fit_phone,
    fmt_ago,
    fmt_dt,
    late_minutes,
    local_times,
    marts_ready,
    q,
    require_db,
    schedule_join,
    table,
)

from ltdwatch.dump import newest_archive_file

require_db()
FV = str(current_fv())  # schedule version in force today
st.title("Diagnostics")
st.caption(
    "Everything here reads the database directly. The Refresh button clears the 60-second cache."
)
if st.button("Refresh"):
    st.cache_data.clear()

# ---- 0. What just came in ------------------------------------------------------------------------
st.subheader("0 · What came in during the last few minutes")
latest = q("""
    select f.feed::text as feed, f.fetched_at, f.header_timestamp, f.entity_count, f.byte_size
    from rt.fetch f
    where f.fetch_id in (select max(fetch_id) from rt.fetch group by feed)
    order by feed
""")
if latest.empty:
    st.error("Nothing has ever been fetched.")
else:
    st.write(
        " · ".join(
            f"**{r['feed']}**: {int(r['entity_count'])} entries, {int(r['byte_size']) // 1024} KB, {fmt_ago(r['fetched_at'])}"
            for _, r in latest.iterrows()
        )
    )

sj, sched = schedule_join("p")
buses = q(f"""
    with ordered as (
        select vp.vehicle_id, vp.trip_id, vp.start_date, vp.position_timestamp, vp.speed_mps,
               vp.current_stop_sequence, vp.stop_id, vp.geom,
               lag(vp.geom) over (partition by vp.vehicle_id order by vp.position_timestamp) as prev_geom,
               lag(vp.position_timestamp) over (partition by vp.vehicle_id order by vp.position_timestamp) as prev_ts,
               row_number() over (partition by vp.vehicle_id order by vp.position_timestamp desc) as rn
        from rt.vehicle_position vp
        where vp.position_timestamp > now() - interval '15 minutes'
    )
    select o.vehicle_id, r.route_short_name as route, t.trip_headsign as headsign,
           s.stop_name as next_stop, coalesce(p.arrival_time, p.departure_time) as next_arrival,
           coalesce(p.arrival_delay, p.departure_delay,
                    extract(epoch from coalesce(p.arrival_time, p.departure_time) - {sched})::int) as arrival_delay,
           o.position_timestamp, round(ST_Distance(o.geom, o.prev_geom)) as moved_m,
           extract(epoch from o.position_timestamp - o.prev_ts) as since_prev_s
    from ordered o
    left join gtfs.trips t  on t.trip_id = o.trip_id and t.feed_version_id = {FV}
    left join gtfs.routes r on r.route_id = t.route_id and r.feed_version_id = t.feed_version_id
    -- LTD's positions name no stop, so "next stop" is the trip's earliest prediction still in the future.
    left join lateral (
        select * from rt.prediction_current x
        where x.trip_id = o.trip_id and x.start_date = o.start_date
          and coalesce(x.arrival_time, x.departure_time) >= o.position_timestamp - interval '30 seconds'
        order by x.stop_sequence limit 1
    ) p on true
    left join gtfs.stops s  on s.stop_id = p.stop_id and s.feed_version_id = t.feed_version_id
    {sj}
    where o.rn = 1
    order by r.route_short_name, o.vehicle_id
""")
if buses.empty:
    st.warning(
        "No bus has reported in the last 15 minutes. Between about midnight and 5 am that's normal; otherwise the poller is down."
    )
else:
    st.markdown(f"**{len(buses)} buses reporting** (latest report each)")
    table(
        pd.DataFrame(
            {
                "Bus": buses["vehicle_id"],
                "Route": buses["route"],
                "Heading": buses["headsign"],
                "Next stop": buses["next_stop"],
                "Predicted there": local_times(buses["next_arrival"]),
                "Min late": late_minutes(buses["arrival_delay"]),
                "Moved since last report": pd.to_numeric(buses["moved_m"]),
                "Reported": local_times(buses["position_timestamp"]),
            }
        ),
        hide_index=True,
        width="stretch",
        column_config={
            "Predicted there": col_time("Predicted there"),
            "Min late": col_late(),
            "Moved since last report": col_minutes("Moved since last report", fmt="%.0f m"),
            "Reported": col_time("Reported"),
        },
    )

revs = q(f"""
    select h.closed_at, r.route_short_name as route, t.trip_headsign as headsign, s.stop_name,
           coalesce(h.arrival_time, h.departure_time) as old_time, coalesce(c.arrival_time, c.departure_time) as new_time
    from rt.prediction_history h
    join rt.prediction_current c using (trip_id, start_date, stop_sequence)
    left join gtfs.trips t  on t.trip_id = h.trip_id and t.feed_version_id = {FV}
    left join gtfs.routes r on r.route_id = t.route_id and r.feed_version_id = t.feed_version_id
    left join gtfs.stops s  on s.stop_id = h.stop_id and s.feed_version_id = t.feed_version_id
    where h.closed_at > now() - interval '5 minutes'
      and h.last_seen_at = (select max(last_seen_at) from rt.prediction_history x
                            where x.trip_id = h.trip_id and x.start_date = h.start_date and x.stop_sequence = h.stop_sequence)
    order by h.closed_at desc limit 25
""")
n_revs = q(
    "select count(*) as n from rt.prediction_history where closed_at > now() - interval '5 minutes'"
)["n"][0]
st.markdown(f"**{int(n_revs):,} predictions revised in the last 5 minutes** — the 25 most recent:")
if not revs.empty:
    revs["old_time"] = pd.to_datetime(revs["old_time"], utc=True)
    revs["new_time"] = pd.to_datetime(revs["new_time"], utc=True)
    revs["shift"] = (revs["new_time"] - revs["old_time"]).dt.total_seconds()
    table(
        pd.DataFrame(
            {
                "When": local_times(revs["closed_at"]),
                "Route": revs["route"],
                "Heading": revs["headsign"],
                "Stop": revs["stop_name"],
                "Was": local_times(revs["old_time"]),
                "Now": local_times(revs["new_time"]),
                "Shift": revs["shift"],
            }
        ),
        hide_index=True,
        width="stretch",
        column_config={
            "When": col_time("When"),
            "Was": col_time("Was"),
            "Now": col_time("Now"),
            "Shift": col_minutes("Shift", fmt="%+.0f s", help="Positive = now predicted later."),
        },
    )

settled = q(f"""
    select r.route_short_name as route, t.trip_headsign as headsign, s.stop_name,
           coalesce(p.arrival_time, p.departure_time) as settled_time, {sched} as scheduled_time, p.last_seen_at,
           extract(epoch from p.last_seen_at - coalesce(p.arrival_time, p.departure_time)) as held_after_s
    from rt.prediction_current p
    join gtfs.trips t  on t.trip_id = p.trip_id and t.feed_version_id = {FV}
    join gtfs.routes r on r.route_id = t.route_id and r.feed_version_id = t.feed_version_id
    left join gtfs.stops s on s.stop_id = p.stop_id and s.feed_version_id = t.feed_version_id
    {sj}
    where coalesce(p.arrival_time, p.departure_time) between now() - interval '30 minutes' and now()
      and p.last_seen_at >= coalesce(p.arrival_time, p.departure_time) + interval '20 seconds'
    order by coalesce(p.arrival_time, p.departure_time) desc limit 40
""")
st.markdown(
    f"**Departures the feed has settled in the last 30 minutes** (method B, live; latest {len(settled)} shown)"
)
if settled.empty:
    st.warning(
        "None. Either no buses are running or the feed does not hold past-stop times the way it appeared to."
    )
else:
    settled["late_s"] = (
        pd.to_datetime(settled["settled_time"], utc=True)
        - pd.to_datetime(settled["scheduled_time"], utc=True)
    ).dt.total_seconds()
    table(
        pd.DataFrame(
            {
                "Left stop": local_times(settled["settled_time"]),
                "Route": settled["route"],
                "Heading": settled["headsign"],
                "Stop": settled["stop_name"],
                "Scheduled": local_times(settled["scheduled_time"]),
                "Min late": late_minutes(settled["late_s"]),
                "Still reported for": pd.to_numeric(settled["held_after_s"]),
            }
        ),
        hide_index=True,
        width="stretch",
        column_config={
            "Left stop": col_time("Left stop"),
            "Scheduled": col_time("Scheduled"),
            "Min late": col_late(),
            "Still reported for": col_minutes("Still reported for", fmt="%.0f s"),
        },
    )
    st.caption(
        "A time the feed kept reporting after it had passed is LTD's record of when the bus left. Scheduled is the feed's own scheduled time for that stop."
    )

if marts_ready():
    derived = q(f"""
        select o.observed_arrival, o.uncertainty_s, o.is_bounded, o.is_plausible,
               r.route_short_name as route, s.stop_name,
               o.scheduled_arrival, extract(epoch from o.observed_arrival - o.scheduled_arrival) as late_s
        from intermediate.int_observed_arrivals o
        join gtfs.routes r on r.route_id = o.route_id and r.feed_version_id = {FV}
        left join gtfs.stops s on s.stop_id = o.stop_id and s.feed_version_id = r.feed_version_id
        order by o.observed_arrival desc limit 25
    """)
    st.markdown(
        f"**Arrivals derived from positions** (method A, as of the last build; latest {len(derived)})"
    )
    if derived.empty:
        st.warning(
            "The last build derived no arrivals. Section 4 below and `make dump` will show why."
        )
    else:
        table(
            pd.DataFrame(
                {
                    "Arrived": local_times(derived["observed_arrival"]),
                    "±": pd.to_numeric(derived["uncertainty_s"]),
                    "Route": derived["route"],
                    "Stop": derived["stop_name"],
                    "Scheduled": local_times(derived["scheduled_arrival"]),
                    "Min late": late_minutes(derived["late_s"]),
                    "Bounded": derived["is_bounded"],
                    "Plausible": derived["is_plausible"],
                }
            ),
            hide_index=True,
            width="stretch",
            column_config={
                "Arrived": col_time("Arrived"),
                "±": col_minutes("±", fmt="%.0f s"),
                "Scheduled": col_time("Scheduled"),
                "Min late": col_late(),
            },
        )

# ---- 1. Is the poller alive? ----------------------------------------------------------------
st.subheader("1 · Poller")
fresh = q("""
    select feed::text as feed, max(fetched_at) as last_fetch,
           count(*) filter (where fetched_at > now() - interval '60 minutes') as fetches_last_hour,
           count(*) filter (where fetched_at > now() - interval '24 hours') as fetches_last_day,
           count(*) as fetches_total
    from rt.fetch group by 1 order by 1
""")
if fresh.empty:
    st.error(
        "No fetches at all. The poller has never written. `docker compose ps` and `make logs`."
    )
else:
    cols = st.columns(len(fresh))
    for col, (_, r) in zip(cols, fresh.iterrows(), strict=False):
        expected = 12 if r["feed"] == "alerts" else 120
        col.metric(
            r["feed"],
            f"{int(r['fetches_last_hour'])} / {expected} last hour",
            f"last {fmt_ago(r['last_fetch'])}",
            delta_color="off",
        )
    st.caption(
        "At a 30 s poll, a healthy hour is ~120 fetches for trip_updates and vehicle_positions and ~12 for alerts. Fewer means the feed didn't change (discarded polls) or the poller was down."
    )

per5 = q("""
    select date_trunc('hour', fetched_at) + interval '5 min' * floor(extract(minute from fetched_at) / 5) as bucket,
           feed::text as feed, count(*) as fetches
    from rt.fetch where fetched_at > now() - interval '6 hours' group by 1, 2 order by 1
""")
if not per5.empty:
    fig = px.bar(per5, x="bucket", y="fetches", color="feed", barmode="group")
    fig.update_layout(xaxis_title="", yaxis_title="fetches per 5 min (expect 10)", legend_title="")
    st.plotly_chart(fit_phone(fig), width="stretch")

unchanged = q(
    "select feed::text as feed, sum(unchanged_count) as discarded from rt.fetch_unchanged where hour > now() - interval '24 hours' group by 1"
)
if not unchanged.empty:
    st.caption(
        "Polls discarded in the last 24 h because the feed hadn't changed: "
        + ", ".join(f"{r['feed']} {int(r['discarded'])}" for _, r in unchanged.iterrows())
    )

# ---- 2. Rows arriving ----------------------------------------------------------------------------
st.subheader("2 · Rows arriving")
growth = q("""
    with est as (
        select s.nspname || '.' || c.relname as tbl, c.reltuples::bigint as total
        from pg_class c join pg_namespace s on s.oid = c.relnamespace
        where s.nspname = 'rt' and c.relkind = 'r'
    ),
    recent as (
        select 'rt.vehicle_position' as tbl,
               (select count(*) from rt.vehicle_position where position_timestamp > now() - interval '60 minutes') as last_hour
        union all
        select 'rt.trip_update', (select count(*) from rt.trip_update where fetch_id in (select fetch_id from rt.fetch where fetched_at > now() - interval '60 minutes'))
        union all
        select 'rt.prediction_current', (select count(*) from rt.prediction_current where last_seen_at > now() - interval '60 minutes')
        union all
        select 'rt.prediction_history', (select count(*) from rt.prediction_history where closed_at > now() - interval '60 minutes')
        union all
        select 'rt.alert', (select count(*) from rt.alert where fetch_id in (select fetch_id from rt.fetch where fetched_at > now() - interval '60 minutes'))
    )
    select recent.tbl, greatest(est.total, 0) as total, recent.last_hour
    from recent left join est using (tbl)
""")
sizes = q("""
    select schemaname || '.' || relname as tbl, pg_size_pretty(pg_total_relation_size(relid)) as on_disk,
           pg_total_relation_size(relid) as bytes
    from pg_catalog.pg_statio_user_tables where schemaname in ('rt', 'gtfs', 'intermediate', 'marts')
""")
growth = growth.merge(sizes[["tbl", "bytes"]], on="tbl", how="left")
table(
    growth.rename(
        columns={
            "tbl": "Table",
            "total": "Rows (approx.)",
            "last_hour": "Touched in last hour",
            "bytes": "On disk",
        }
    ),
    hide_index=True,
    width="content",
    column_config={
        "Rows (approx.)": st.column_config.NumberColumn("Rows (approx.)", format="localized"),
        "Touched in last hour": st.column_config.NumberColumn(
            "Touched in last hour", format="localized"
        ),
        "On disk": st.column_config.NumberColumn("On disk", format="bytes"),
    },
)
st.caption(
    "vehicle_position gains one row per new position report (roughly 50 per active bus per hour in LTD's feed); prediction_current 'touched' counts trip-stops whose prediction was re-asserted or revised."
)

raw_dir = Path(os.environ.get("RAW_ARCHIVE_DIR", "./data/raw"))
if raw_dir.exists():
    now_s = datetime.now(tz=UTC).timestamp()
    parts = []
    for feed in ("trip_updates", "vehicle_positions", "alerts"):
        newest = newest_archive_file(raw_dir / feed)
        if newest is not None:
            age_min = (now_s - int(newest.name.split(".")[0])) / 60
            parts.append(f"{feed} {age_min:.0f} min ago")
    st.caption(
        f"Raw archive under `{raw_dir}`, newest message per feed: " + (", ".join(parts) or "none")
    )

total_disk = q("select pg_size_pretty(pg_database_size(current_database())) as size")
st.caption(f"Whole database on disk: {total_disk['size'][0]}.")

# ---- 3. What the feed actually contains ------------------------------------------------------------
st.subheader("3 · What LTD's feed gives us")
st.caption(
    "LTD's positions carry no stop fields, so arrivals are derived geometrically from positions "
    "along the route shape (see Data & methods). These are the fields the feed does and doesn't fill."
)
fields = q("""
    select count(*) as positions,
           count(*) filter (where current_stop_sequence is not null) as with_stop_sequence,
           count(*) filter (where stop_id is not null) as with_stop_id,
           count(*) filter (where bearing is not null) as with_bearing,
           count(*) filter (where speed_mps is not null) as with_speed,
           count(*) filter (where trip_id is null) as without_trip,
           count(distinct vehicle_id) as vehicles
    from rt.vehicle_position where position_timestamp > now() - interval '24 hours'
""")
r = fields.iloc[0]
n = int(r["positions"]) or 1
c1, c2, c3, c4 = st.columns(4)
c1.metric("Positions, last 24 h", f"{int(r['positions']):,}")
c2.metric("…with a heading", f"{100 * int(r['with_bearing']) / n:.0f}%")
c3.metric("…with a speed", f"{100 * int(r['with_speed']) / n:.0f}%")
c4.metric("…with no trip", f"{100 * int(r['without_trip']) / n:.0f}%")
st.caption(
    f"With a stop sequence: {int(r['with_stop_sequence']):,}; with a stop id: {int(r['with_stop_id']):,}. "
    "Both are expected to be zero for LTD."
)

preds = q("""
    select count(*) as n, count(*) filter (where arrival_time is not null) as with_time,
           count(*) filter (where departure_time is not null) as with_departure,
           count(*) filter (where coalesce(arrival_delay, departure_delay) is not null) as with_delay,
           count(*) filter (where scheduled_time is not null) as with_scheduled,
           count(*) filter (where schedule_relationship = 1) as skipped
    from rt.prediction_current where last_seen_at > now() - interval '24 hours'
""")
occ = q("""
    select count(*) filter (where occupancy_status is not null) as with_occ, count(*) as n
    from rt.vehicle_position where position_timestamp > now() - interval '24 hours'
""").iloc[0]
st.caption(
    f"Positions carrying an occupancy reading: {int(occ['with_occ']):,} of {int(occ['n']):,} (how full the bus is — a feature for later)."
)
pr = preds.iloc[0]
st.caption(
    f"Predictions in the last 24 h: {int(pr['n']):,}; with an arrival time {int(pr['with_time']):,}; with a delay field {int(pr['with_delay']):,}; with the feed's own scheduled time {int(pr['with_scheduled']):,}; marked skipped {int(pr['skipped']):,}."
)

# ---- 4. Analysis layer --------------------------------------------------------------------------------
st.subheader("4 · Analysis layer (dbt)")
if not marts_ready():
    st.warning(
        "Never built. Run `make dbt-build` once, then `make scheduler` to keep it rebuilding every 15 minutes."
    )
else:
    log = (
        q("""
        select started_at, finished_at, n_errors, n_warnings from analytics.build_log order by started_at desc limit 5
    """)
        if int(
            q(
                "select count(*) as n from information_schema.tables where table_schema = 'analytics' and table_name = 'build_log'"
            )["n"][0]
        )
        else pd.DataFrame()
    )
    if log.empty:
        st.caption(
            "No build log yet (it starts with the first build after the scheduler files are in place)."
        )
    else:
        last = log.iloc[0]
        st.metric(
            "Last build",
            fmt_dt(last["finished_at"] or last["started_at"]),
            f"{fmt_ago(last['finished_at'] or last['started_at'])}"
            + ("" if last["finished_at"] else " — still running or failed"),
            delta_color="off",
        )
        table(
            pd.DataFrame(
                {
                    "Started": local_times(log["started_at"]),
                    "Finished": local_times(log["finished_at"]),
                    "Errors": pd.to_numeric(log["n_errors"]),
                    "Warnings": pd.to_numeric(log["n_warnings"]),
                }
            ),
            hide_index=True,
            width="content",
            column_config={
                "Started": st.column_config.DatetimeColumn(
                    "Started", format="MMM D, h:mm a", timezone="America/Los_Angeles"
                ),
                "Finished": st.column_config.DatetimeColumn(
                    "Finished", format="MMM D, h:mm a", timezone="America/Los_Angeles"
                ),
            },
        )
    st.caption(
        "The dashboard shows what the last build computed. New positions don't appear in reliability numbers until the next build (every 15 minutes with `make scheduler`)."
    )

    layer = q("""
        select 'intermediate.int_scheduled_stop_events' as tbl, count(*) as rows, min(service_date)::text as first_day, max(service_date)::text as last_day from intermediate.int_scheduled_stop_events
        union all
        select 'intermediate.int_observed_arrivals', count(*), min(service_date)::text, max(service_date)::text from intermediate.int_observed_arrivals
        union all
        select 'marts.fct_stop_events (scored)', count(*) filter (where status is not null), min(service_date)::text, max(service_date)::text from marts.fct_stop_events
        union all
        select 'marts.fct_prediction_errors', count(*), min(service_date)::text, max(service_date)::text from marts.fct_prediction_errors
        union all
        select 'marts.mart_route_daily', count(*), min(service_date)::text, max(service_date)::text from marts.mart_route_daily
    """)
    table(
        layer.rename(
            columns={
                "tbl": "Table",
                "rows": "Rows",
                "first_day": "First day",
                "last_day": "Last day",
            }
        ),
        hide_index=True,
        width="content",
    )
    obs_today = q("""
        select count(*) as n, count(*) filter (where is_bounded and is_plausible) as bounded,
               percentile_cont(0.5) within group (order by uncertainty_s) as median_uncertainty_s
        from intermediate.int_observed_arrivals where service_date >= current_date - 1
    """).iloc[0]
    st.write(
        f"Observed arrivals since yesterday: **{int(obs_today['n']):,}**, of which **{int(obs_today['bounded']):,}** bounded "
        f"(usable), median uncertainty ±{float(obs_today['median_uncertainty_s'] or 0):.0f} s."
    )
    if int(obs_today["n"]) == 0:
        st.error(
            "No observed arrivals were derived since yesterday. If positions are arriving (section 2), check `make dump` and the scheduler's dbt log."
        )
