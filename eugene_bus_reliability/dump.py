"""Diagnostic snapshot: `python -m eugene_bus_reliability dump` (or `make dump`, which writes dump.txt).

Collection health, feed shape, what change-only storage saves, the state of the
analysis layer, anomalies, and recent container errors, in about 100 lines.
`--raw` adds the newest raw feed message of each kind.
"""

from __future__ import annotations

import gzip
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import psycopg
from google.protobuf import text_format

from eugene_bus_reliability import rt_parse
from eugene_bus_reliability.config import Settings
from eugene_bus_reliability.signed import SIGNED_COUNT_SUMS, signed_percentile
from eugene_bus_reliability.stop_order import route_stop_order

# Must match the dbt macro horizon_band() and app/common.py HORIZON_BANDS.
HORIZON_BAND_SQL = (
    "case when horizon_min <= 1 then 1 when horizon_min <= 3 then 3 when horizon_min <= 6 then 6 "
    "when horizon_min <= 10 then 10 when horizon_min <= 15 then 15 when horizon_min <= 20 then 20 "
    "else 30 end"
)


def section(title: str) -> None:
    print(f"\n== {title}")


def show(cur: psycopg.Cursor, sql: str, params: tuple = (), limit: int = 30) -> None:
    try:
        cur.execute(sql, params)
        if cur.description is None:
            return
        cols = [d.name for d in cur.description]
        rows = cur.fetchmany(limit)
        print(" | ".join(cols))
        for r in rows:
            print(
                " | ".join(
                    "∅" if v is None else (f"{v:.3g}" if isinstance(v, float) else str(v))
                    for v in r
                )
            )
        if not rows:
            print("(no rows)")
    except Exception as exc:  # noqa: BLE001
        print(f"!! {str(exc).strip().splitlines()[0][:200]}")
        cur.connection.rollback()


def container_logs(service: str, lines: int = 200) -> list[str]:
    try:
        out = subprocess.run(
            ["docker", "compose", "logs", "--no-color", "--tail", str(lines), service],
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout
    except Exception as exc:  # noqa: BLE001
        return [f"(could not read logs: {exc})"]
    keep = ("ERROR", "WARNING", "FAIL", "Traceback")
    return [
        ln
        for ln in out.splitlines()
        if any(k in ln for k in keep)
        # dbt's summary line of a clean build contains "ERROR=0"; that is not an error
        and not ("Done. PASS=" in ln and "ERROR=0" in ln and "WARN=0" in ln)
    ]


def newest_archive_file(feed_dir: Path) -> Path | None:
    d = feed_dir
    for _ in range(3):  # year, month, day
        if not d.is_dir():
            return None
        subdirs = sorted(p for p in d.iterdir() if p.is_dir())
        if not subdirs:
            return None
        d = subdirs[-1]
    files = sorted(d.glob("*.pb.gz"))
    return files[-1] if files else None


def archive_status(settings: Settings, cur: psycopg.Cursor) -> None:
    root = settings.raw_archive_dir
    now = datetime.now(tz=UTC).timestamp()
    cur.execute(
        "select feed::text, extract(epoch from max(header_timestamp)) from rt.fetch "
        "where archive_path is not null group by 1"
    )
    db_newest = {feed: float(ts) for feed, ts in cur.fetchall()}
    for feed in ("trip_updates", "vehicle_positions", "alerts"):
        newest = newest_archive_file(root / feed)
        if newest is None:
            print(f"raw archive, {feed}: no files under {root / feed}")
            continue
        file_ts = int(newest.name.split(".")[0])
        print(
            f"raw archive, {feed}: newest {newest.relative_to(root)} ({(now - file_ts) / 60:.0f} min old)"
        )
        behind_min = (db_newest.get(feed, file_ts) - file_ts) / 60
        if behind_min > 5:
            print(
                f"!! the archive is {behind_min:.0f} min behind the database for {feed}: new files are "
                f"not reaching {root}. Recreate the poller: docker compose up -d --force-recreate poller"
            )


def raw_samples(settings: Settings) -> None:
    section("RAW FEED MESSAGES (newest archived message per feed, first entity)")
    root = settings.raw_archive_dir
    for feed in ("vehicle_positions", "trip_updates", "alerts"):
        newest = newest_archive_file(root / feed)
        if newest is None:
            print(f"-- {feed}: no archived files")
            continue
        with gzip.open(newest, "rb") as f:
            msg = rt_parse.parse_feed(f.read())
        print(f"-- {feed}: {newest.name}, entities={len(msg.entity)}")
        if msg.entity:
            print(text_format.MessageToString(msg.entity[0], as_one_line=False).rstrip())


def coverage_by_hour(settings: Settings, cur: psycopg.Cursor) -> None:
    """Hour by hour over the last 4 days (Eugene time): what we fetched and stored, how often
    a bus went over 4 minutes without a stored report, how many mid-trip stops went unscored,
    and whether an analysis rebuild or a restart of ours was running then. Lines up a day with
    low coverage against our own deploys (ours) or not (LTD's), and shows whether the raw
    archive holds messages the database lacks (then `make replay` recovers them)."""
    section(
        "COVERAGE BY HOUR, last 4 days (Eugene time): our side (fetches, restarts, rebuilds) or LTD's?"
    )
    print(
        "fetches = vehicle-position messages stored; archived = files in the raw archive for that\n"
        "hour (more than fetches: messages the database lacks; `make replay` stores them);\n"
        "gaps_4min_pct = share of a bus's reports that came over 4 min after its previous one;\n"
        "unscored_mid_pct = mid-trip stops of trips in the feed left unscored; full_rebuild = an\n"
        "analysis full rebuild (after a deploy) was running that hour."
    )
    tz = "America/Los_Angeles"
    cur.execute(
        f"""
        with f as (
            select date_trunc('hour', fetched_at at time zone '{tz}') hr, count(*) fetches
            from rt.fetch
            where feed = 'vehicle_positions' and fetched_at > now() - interval '4 days'
            group by 1
        ),
        g as (
            select date_trunc('hour', position_timestamp at time zone '{tz}') hr,
                   count(*) positions, count(distinct vehicle_id) buses,
                   round(100.0 * avg((gap > interval '4 minutes')::int), 1) gaps_4min_pct
            from (
                select vehicle_id, position_timestamp,
                       position_timestamp - lag(position_timestamp) over (
                           partition by vehicle_id order by position_timestamp) gap
                from rt.vehicle_position
                where position_timestamp > now() - interval '4 days'
            ) x
            where gap is null or gap < interval '1 hour'
            group by 1
        ),
        e as (
            select date_trunc('hour', scheduled_arrival at time zone '{tz}') hr,
                   round(100.0 * avg((status is null)::int), 1) unscored_mid_pct
            from marts.fct_stop_events
            where service_date >= current_date - 4 and trip_had_realtime
              and not is_first_stop and not is_last_stop
              and scheduled_arrival < now() - interval '2 hours'
            group by 1
        ),
        b as (
            select started_at at time zone '{tz}' s,
                   coalesce(finished_at, now()) at time zone '{tz}' t
            from analytics.build_log
            where full_refresh and started_at > now() - interval '5 days'
        )
        select to_char(g.hr, 'Dy MM-DD HH24') hour_, coalesce(f.fetches, 0), g.positions,
               g.buses, g.gaps_4min_pct, e.unscored_mid_pct,
               exists (select 1 from b where b.s < g.hr + interval '1 hour' and b.t > g.hr)
                   full_rebuild,
               g.hr
        from g left join f using (hr) left join e using (hr)
        where extract(hour from g.hr) between 5 and 23
        order by g.hr
        """
    )
    rows = cur.fetchall()
    # raw archive files per Eugene hour (the archive is filed by UTC day)
    archived: dict[datetime, int] = {}
    root = settings.raw_archive_dir / "vehicle_positions"
    if root.is_dir() and rows:
        from zoneinfo import ZoneInfo

        local = ZoneInfo(tz)
        days_utc = set()
        for r in rows:
            h = r[7].replace(tzinfo=local).astimezone(UTC)
            days_utc.add(h.date())
        for d in sorted(days_utc):
            folder = root / f"{d:%Y/%m/%d}"
            if not folder.is_dir():
                continue
            for f in folder.glob("*.pb.gz"):
                try:
                    ts = int(f.name.split(".")[0])
                except ValueError:
                    continue
                hr = (
                    datetime.fromtimestamp(ts, tz=UTC)
                    .astimezone(local)
                    .replace(minute=0, second=0, microsecond=0, tzinfo=None)
                )
                archived[hr] = archived.get(hr, 0) + 1
    print(
        "hour | fetches | archived | positions | buses | gaps_4min_pct | unscored_mid_pct | "
        "full_rebuild"
    )
    for hour_, fetches, positions, buses, gaps, unscored, rebuild, hr in rows:
        arch = archived.get(hr) if archived else None
        flag = "  <-- archive has more" if arch and arch > fetches + 2 else ""
        print(
            f"{hour_} | {fetches} | {'∅' if arch is None else arch} | {positions} | {buses} | "
            f"{gaps} | {'∅' if unscored is None else unscored} | {'yes' if rebuild else ''}{flag}"
        )


def route_order_check(conn: psycopg.Connection, cur: psycopg.Cursor) -> None:
    """Routes whose trips don't all make the same stops, and how the stop order the site uses
    (stop_order.py) handles them: places in the merged order, stops a trip visits twice (a
    loop), and how many stops the old order (each stop's smallest stop number) put elsewhere."""
    section(
        "ROUTE STOP ORDER: routes whose trips make different stops (short trips, loops), as the "
        "site orders them"
    )
    print(
        "stop_lists = distinct stop lists among the trips; longest = stops on the longest;\n"
        "places = places in the merged order (a stop visited twice counts twice);\n"
        "twice = stops a single trip visits twice (loops); old_order_moved = stops whose place\n"
        "differs from where ordering by their smallest stop number put them (the old way)."
    )
    try:
        fv = cur.execute("select max(feed_version_id) from gtfs.feed_version").fetchone()[0]
        rows = cur.execute(
            """
            select r.route_id, r.route_short_name, t.direction_id,
                   count(distinct t.trip_id) as trips
            from gtfs.trips t
            join gtfs.routes r on r.route_id = t.route_id and r.feed_version_id = t.feed_version_id
            where t.feed_version_id = %s and t.direction_id is not null
            group by 1, 2, 3
            """,
            (fv,),
        ).fetchall()
        out = []
        for route_id, short, direction, trips in rows:
            lists = cur.execute(
                """
                select count(distinct pattern), max(n), bool_or(twice)
                from (
                    select st.trip_id, string_agg(st.stop_id::text, ',' order by st.stop_sequence)
                               as pattern,
                           count(*) as n,
                           count(*) > count(distinct st.stop_id) as twice
                    from gtfs.stop_times st
                    join gtfs.trips t on t.trip_id = st.trip_id
                     and t.feed_version_id = st.feed_version_id
                    where st.feed_version_id = %s and t.route_id = %s and t.direction_id = %s
                    group by 1
                ) x
                """,
                (fv, route_id, direction),
            ).fetchone()
            if lists[0] <= 1 and not lists[2]:
                continue
            order, _ = route_stop_order(conn, fv, route_id, direction)
            old = cur.execute(
                """
                select st.stop_id::text from gtfs.stop_times st
                join gtfs.trips t on t.trip_id = st.trip_id and t.feed_version_id = st.feed_version_id
                where st.feed_version_id = %s and t.route_id = %s and t.direction_id = %s
                group by 1 order by min(st.stop_sequence), 1
                """,
                (fv, route_id, direction),
            ).fetchall()
            first: list[str] = []
            for sid in order:
                if sid not in first:
                    first.append(sid)
            moved = sum(1 for a, (b,) in zip(first, old, strict=False) if a != b)
            twice = len(order) - len(set(order))
            out.append((str(short), direction, trips, lists[0], lists[1], len(order), twice, moved))
        print("route | dir | trips | stop_lists | longest | places | twice | old_order_moved")
        for r in sorted(out, key=lambda r: (len(r[0]), r[0], r[1])):
            print(" | ".join(str(v) for v in r))
        if not out:
            print("(every route's trips make the same stops)")
    except Exception as exc:  # noqa: BLE001
        print(f"!! {str(exc).strip().splitlines()[0][:200]}")
        conn.rollback()


def prediction_strips_check(cur: psycopg.Cursor) -> None:
    """The Predictions page's 8-in-10 strips come from running counts in the marts
    (straight-line between thresholds); here they are next to the exact percentiles from the
    samples, for the busiest routes over the last 30 days."""
    section(
        "PREDICTION STRIPS (Predictions page tables): from the marts' running counts vs exact, "
        "last 30 days, seconds (negative = earlier than told)"
    )
    show(
        cur,
        """
        with s as (
            select route_id, 'sign 5 min' as what, error_s as e
            from marts.fct_countdown_samples
            where in_comparison and ahead_min = 5
              and service_date >= (now() at time zone 'America/Los_Angeles')::date - 29
            union all
            select route_id, 'timetable', schedule_error_s
            from marts.fct_countdown_samples
            where in_comparison and ahead_min = 15
              and service_date >= (now() at time zone 'America/Los_Angeles')::date - 29
        )
        select r.route_short_name as route, what, count(*) as n,
               round(percentile_cont(0.1) within group (order by e)::numeric) as p10,
               round(percentile_cont(0.5) within group (order by e)::numeric) as p50,
               round(percentile_cont(0.9) within group (order by e)::numeric) as p90,
               round(avg(abs(e))::numeric) as mean_abs
        from s
        join gtfs.routes r on r.route_id = s.route_id
         and r.feed_version_id = (select max(feed_version_id) from gtfs.feed_version)
        group by 1, 2 order by count(*) desc, 1, 2 limit 16
        """,
    )
    print("The same from the running counts (n_sle_ columns), as the site reads them:")
    cols = SIGNED_COUNT_SUMS
    try:
        rows = cur.execute(
            f"""
            select r.route_short_name as route,
                   case when basis = 'sign' then 'sign 5 min' else 'timetable' end as what,
                   sum(n) as n, {cols}
            from marts.mart_prediction_daily m
            join gtfs.routes r on r.route_id = m.route_id
             and r.feed_version_id = (select max(feed_version_id) from gtfs.feed_version)
            where (basis = 'timetable' or ahead_min = 5)
              and service_date >= (now() at time zone 'America/Los_Angeles')::date - 29
            group by 1, 2 order by sum(n) desc, 1, 2 limit 16
            """
        ).fetchall()
        names = [d.name for d in cur.description]
        print("route | what | n | p10 | p50 | p90")
        for values in rows:
            r = dict(zip(names, values, strict=True))
            print(
                " | ".join(
                    [str(r["route"]), r["what"], str(r["n"])]
                    + [str(round(signed_percentile(r, q))) for q in (0.1, 0.5, 0.9)]
                )
            )
    except Exception as exc:  # noqa: BLE001
        print(f"!! {str(exc).strip().splitlines()[0][:200]}")
        cur.connection.rollback()


def main(settings: Settings, raw: bool = False) -> None:
    print("Eugene Bus Watch dump (concise)")
    if raw:
        raw_samples(settings)

    with psycopg.connect(settings.database_url) as conn, conn.cursor() as cur:
        section(
            "HEALTH — poller (at a 30 s poll: ~120/h for trip_updates & vehicle_positions, ~12/h alerts)"
        )
        show(
            cur,
            """
            select feed::text feed,
                   count(*) filter (where fetched_at > now() - interval '1 hour') last_hour,
                   count(*) filter (where fetched_at > now() - interval '24 hours') last_24h,
                   count(*) total,
                   round(extract(epoch from now() - max(fetched_at)) / 60) min_since_last,
                   min(fetched_at)::date first_day
            from rt.fetch group by 1 order by 1
        """,
        )
        show(
            cur,
            """
            select coalesce(sum(unchanged_count), 0) discarded_unchanged_24h from rt.fetch_unchanged
            where hour > now() - interval '24 hours'
        """,
        )
        show(
            cur,
            """
            select pg_size_pretty(pg_database_size(current_database())) db_size,
                   (select count(*) from gtfs.feed_version) schedule_versions,
                   (select max(feed_end_date)::text from gtfs.feed_version) schedule_valid_to
        """,
        )
        archive_status(settings, cur)

        section("CLOCKS & LIVE FRESHNESS (what Live map, Arrivals and Overview depend on)")
        cur.execute("select now()")
        db_now = cur.fetchone()[0]
        host_now = datetime.now(tz=UTC)
        print(
            f"host clock (UTC): {host_now:%Y-%m-%d %H:%M:%S} · database clock (UTC): "
            f"{db_now:%Y-%m-%d %H:%M:%S} · database minus host: "
            f"{(db_now - host_now).total_seconds():+.0f} s"
        )
        show(
            cur,
            """
            select feed::text feed,
                   round(extract(epoch from now() - max(fetched_at))) s_since_fetch,
                   round(extract(epoch from now() - max(header_timestamp))) s_since_header,
                   (array_agg(round(extract(epoch from fetched_at - header_timestamp)) order by fetched_at desc))[1:3] fetch_minus_header_s_last3,
                   (array_agg(entity_count order by fetched_at desc))[1:3] entities_last3
            from rt.fetch where fetched_at > now() - interval '2 days' group by 1 order by 1
        """,
        )
        show(
            cur,
            """
            select round(extract(epoch from now() - max(vp.position_timestamp))) s_since_newest_position,
                   count(*) filter (where vp.position_timestamp between now() - interval '10 minutes' and now() + interval '5 minutes') positions_in_live_window,
                   count(*) filter (where f.fetched_at > now() - interval '10 minutes') positions_stored_last_10min,
                   count(distinct vp.vehicle_id) filter (where f.fetched_at > now() - interval '10 minutes') buses_stored_last_10min,
                   round(percentile_cont(0.5) within group (order by extract(epoch from f.fetched_at - vp.position_timestamp))
                         filter (where f.fetched_at > now() - interval '1 hour')) median_report_age_at_fetch_s,
                   round(min(extract(epoch from f.fetched_at - vp.position_timestamp)) filter (where f.fetched_at > now() - interval '1 hour')) min_report_age_s,
                   round(max(extract(epoch from f.fetched_at - vp.position_timestamp)) filter (where f.fetched_at > now() - interval '1 hour')) max_report_age_s
            from rt.vehicle_position vp join rt.fetch f on f.fetch_id = vp.fetch_id
            where vp.position_timestamp > now() - interval '1 day'
        """,
        )
        show(
            cur,
            """
            with fresh as (
                select *, coalesce(arrival_time, departure_time) as t from rt.prediction_current
                where last_seen_at > now() - interval '3 minutes'
            )
            select (select round(extract(epoch from now() - max(last_seen_at))) from rt.prediction_current) s_since_prediction_update,
                   count(*) predictions_updated_last_3min,
                   count(*) filter (where t between now() and now() + interval '5 minutes') due_next_5min,
                   count(*) filter (where t between now() and now() + interval '15 minutes') due_next_15min,
                   count(*) filter (where t between now() - interval '5 minutes' and now()) passed_last_5min,
                   round(extract(epoch from min(t) filter (where t > now()) - now()) / 60, 1) nearest_future_min,
                   round(percentile_cont(0.5) within group (order by extract(epoch from t - now()) / 60)) median_min_from_now
            from fresh
        """,
        )
        show(
            cur,
            """
            select ((now() at time zone 'America/Los_Angeles') - interval '3 hours')::date service_date_today,
                   (select max(feed_version_id) from gtfs.feed_version) newest_schedule_version,
                   (select feed_version_id from intermediate.int_feed_version_by_date
                     where service_date = ((now() at time zone 'America/Los_Angeles') - interval '3 hours')::date) schedule_version_in_force,
                   (select count(distinct p.trip_id) from rt.prediction_current p
                     where p.last_seen_at > now() - interval '3 minutes') trips_predicted_now,
                   (select count(distinct p.trip_id) from rt.prediction_current p
                     join gtfs.trips t on t.trip_id = p.trip_id
                      and t.feed_version_id = coalesce(
                          (select feed_version_id from intermediate.int_feed_version_by_date
                            where service_date = ((now() at time zone 'America/Los_Angeles') - interval '3 hours')::date),
                          (select max(feed_version_id) from gtfs.feed_version))
                     where p.last_seen_at > now() - interval '3 minutes') of_which_match_schedule
        """,
        )

        try:
            cur.execute(
                """
                select
                  (select sum(entity_count) from (
                      select distinct on (feed) entity_count from rt.fetch
                      where feed in ('vehicle_positions', 'trip_updates') and fetched_at > now() - interval '2 days'
                      order by feed, fetched_at desc) l) as entities_in_latest,
                  (select to_char(max(fetched_at) at time zone 'America/Los_Angeles', 'Mon DD HH24:MI')
                     from rt.fetch where feed = 'vehicle_positions' and entity_count > 0) as last_nonempty_local,
                  case when to_regclass('intermediate.int_scheduled_stop_events') is null then null
                       else (select count(*) from intermediate.int_scheduled_stop_events
                             where scheduled_arrival between now() - interval '10 minutes' and now() + interval '10 minutes')
                  end as scheduled_stop_events_now
                """
            )
            entities, last_nonempty, scheduled = cur.fetchone()
            print(
                f"latest messages hold {entities} entities in total; last message with buses in it: "
                f"{last_nonempty} (Eugene); scheduled stop events within 10 min of now: {scheduled}"
            )
            if entities == 0 and (scheduled or 0) > 0:
                print(
                    "!! LTD's feed is EMPTY while buses are scheduled: an outage on LTD's side, not ours. "
                    f"No buses or predictions since {last_nonempty}."
                )
        except Exception as exc:  # noqa: BLE001
            print(f"!! {str(exc).strip().splitlines()[0][:200]}")
            cur.connection.rollback()

        section("EMPTY-FEED TIME during scheduled service, by service day (LTD-side outages)")
        show(
            cur,
            """
            with span as (
                select service_date, min(scheduled_arrival) as first_stop, max(scheduled_arrival) as last_stop
                from intermediate.int_scheduled_stop_events group by 1
            )
            select s.service_date,
                   count(f.fetch_id) vehicle_msgs_in_service_hours,
                   count(f.fetch_id) filter (where f.entity_count = 0) empty_msgs,
                   round(count(f.fetch_id) filter (where f.entity_count = 0) * 0.5) approx_empty_minutes,
                   to_char(min(f.fetched_at) filter (where f.entity_count = 0) at time zone 'America/Los_Angeles', 'HH24:MI') first_empty,
                   to_char(max(f.fetched_at) filter (where f.entity_count = 0) at time zone 'America/Los_Angeles', 'HH24:MI') last_empty
            from span s
            join rt.fetch f on f.feed = 'vehicle_positions' and f.fetched_at between s.first_stop and s.last_stop
            group by 1 order by 1
        """,
        )

        section("COLLECTION by service day (positions, buses, trips, prediction rows touched)")
        show(
            cur,
            """
            select vp.start_date service_date, count(*) positions, count(distinct vehicle_id) buses,
                   count(distinct trip_id) trips,
                   (select count(*) from rt.prediction_history h where h.start_date = vp.start_date) superseded_predictions
            from rt.vehicle_position vp where start_date is not null group by 1 order by 1
        """,
        )

        section("FEED SHAPE (last 24h)")
        show(
            cur,
            """
            select count(*) positions,
                   round(100.0 * count(bearing) / count(*)) pct_bearing,
                   round(100.0 * count(speed_mps) / count(*)) pct_speed,
                   round(100.0 * count(occupancy_status) / count(*)) pct_occupancy,
                   count(current_stop_sequence) with_stop_seq, count(stop_id) with_stop_id
            from rt.vehicle_position where position_timestamp > now() - interval '24 hours'
        """,
        )
        show(
            cur,
            """
            select count(*) predictions, count(arrival_time) with_arrival, count(departure_time) with_departure,
                   count(scheduled_time) with_feed_scheduled, count(*) filter (where schedule_relationship = 1) skipped
            from rt.prediction_current where last_seen_at > now() - interval '24 hours'
        """,
        )
        show(
            cur,
            """
            select count(distinct p.trip_id) realtime_trips_not_in_schedule,
                   count(distinct p.trip_id) filter (where t.route_id is null and p.trip_id !~ '^[0-9]+$') non_numeric_ids
            from rt.trip_update p
            left join gtfs.trips t on t.trip_id = p.trip_id and t.feed_version_id = (select max(feed_version_id) from gtfs.feed_version)
            where t.trip_id is null and p.fetch_id in (select fetch_id from rt.fetch where fetched_at > now() - interval '24 hours')
        """,
        )

        section("CHANGE-ONLY STORAGE (prediction rows touched in the last 24 h)")
        show(
            cur,
            """
            with touched as (
                select first_seen_at, last_seen_at from rt.prediction_current
                where last_seen_at > now() - interval '24 hours'
                union all
                select first_seen_at, last_seen_at from rt.prediction_history
                where closed_at > now() - interval '24 hours'
            )
            select count(*) stored_rows,
                   sum(n) rows_if_every_message,
                   round(sum(n)::numeric / nullif(count(*), 0), 1) ratio
            from touched t
            cross join lateral (
                select count(*) n from rt.fetch f
                where f.feed = 'trip_updates'
                  and f.header_timestamp between t.first_seen_at and t.last_seen_at
            ) m
        """,
        )

        section("ANALYSIS LAYER")
        show(
            cur,
            """
            select started_at, finished_at, round(extract(epoch from finished_at - started_at)) seconds,
                   n_errors errors, n_warnings warnings, full_refresh
            from analytics.build_log order by started_at desc limit 4
        """,
        )
        print("Share of the last 24 h the database spent on builds, and the typical build:")
        show(
            cur,
            """
            select count(*) builds,
                   round(100 * sum(extract(epoch from finished_at - started_at)) / 86400) pct_of_day_building,
                   round(percentile_cont(0.5) within group (order by extract(epoch from finished_at - started_at))) median_s,
                   round(max(extract(epoch from finished_at - started_at))) max_s
            from analytics.build_log
            where started_at > now() - interval '24 hours' and finished_at is not null
              and not coalesce(full_refresh, false)
        """,
        )
        print(
            "Slowest steps of an ordinary build (median over the last 24 h, full rebuilds left out):"
        )
        show(
            cur,
            """
            select s.node, s.kind,
                   round(percentile_cont(0.5) within group (order by s.seconds)::numeric, 1) median_s,
                   round(max(s.seconds)::numeric, 1) max_s, count(*) builds
            from analytics.build_step_log s
            join analytics.build_log b using (invocation_id)
            where b.started_at > now() - interval '24 hours' and not coalesce(b.full_refresh, false)
            group by 1, 2 order by 3 desc limit 12
        """,
        )
        print("Slowest steps of the latest build (models and tests):")
        show(
            cur,
            """
            select node, kind, round(seconds::numeric, 1) seconds, status
            from analytics.build_step_log
            where invocation_id = (select invocation_id from analytics.build_log
                                   where finished_at is not null order by started_at desc limit 1)
            order by seconds desc limit 12
        """,
        )
        print(
            "Anything other than a build that the database has been running for over 1 minute (normally empty):"
        )
        show(
            cur,
            """
            select pid, usename login, date_trunc('second', now() - xact_start) running_for, state,
                   left(regexp_replace(query, '\\s+', ' ', 'g'), 80) query
            from pg_stat_activity
            where pid <> pg_backend_pid() and xact_start < now() - interval '1 minute'
              and query not ilike '%%dbt%%' and backend_type = 'client backend'
            order by xact_start
        """,
        )
        show(
            cur,
            """
            select (select count(*) from intermediate.int_scheduled_stop_events) scheduled_events,
                   (select count(*) from intermediate.int_positions_along_shape) positions_along_shape,
                   (select count(*) from intermediate.int_observed_arrivals) observed_arrivals,
                   (select count(*) from marts.fct_stop_events where status is not null) scored_events,
                   (select count(*) from marts.fct_prediction_errors) scored_predictions
        """,
        )
        show(
            cur,
            """
            select service_date, trips_scheduled sched_trips, trips_seen, stop_events_scheduled sched_events,
                   stop_events_observed scored,
                   round(100.0 * stop_events_observed / nullif(stop_events_scheduled, 0)) pct_scored,
                   round(100.0 * trips_reported / nullif(trips_due, 0), 1) pct_trips_reported,
                   round(100.0 * mid_stops_timed / nullif(mid_stops_due, 0), 1) pct_mid_stops_timed,
                   stop_events_implausible implausible,
                   stop_events_doubtful doubtful,
                   stop_events_imprecise imprecise,
                   round(gap_minutes) feed_gap_min
            from marts.mart_daily_coverage order by 1
        """,
        )
        print(
            "Why scheduled stop events weren't scored (stops due more than 2 h ago; one reason each,"
        )
        print(
            "checked left to right): trip never in LTD's feed / feed marked the stop skipped / a trip's"
        )
        print(
            "first stop / its last stop / bus never reached it in the data / passed before the trip's"
        )
        print(
            "first report / implausible time / doubtful stop (int_doubtful_stops) / imprecise time /"
        )
        print("anything else")
        show(
            cur,
            """
            select service_date, count(*) sched,
                   round(100.0 * count(*) filter (where r = 'scored') / count(*), 1) pct_scored,
                   count(*) filter (where r = 'no_rt') trip_not_in_feed,
                   count(*) filter (where r = 'skipped') skipped,
                   count(*) filter (where r = 'first') first_stop,
                   count(*) filter (where r = 'last') last_stop,
                   count(*) filter (where r = 'never') never_reached,
                   count(*) filter (where r = 'before') before_first_report,
                   count(*) filter (where r = 'implausible') implausible,
                   count(*) filter (where r = 'doubtful') doubtful,
                   count(*) filter (where r = 'imprecise') imprecise,
                   count(*) filter (where r = 'other') other
            from (
                select service_date,
                       case when status is not null then 'scored'
                            when not trip_had_realtime then 'no_rt'
                            when was_skipped then 'skipped'
                            when is_first_stop then 'first'
                            when is_last_stop then 'last'
                            when observed_arrival is null then 'never'
                            when not is_bounded then 'before'
                            when not is_plausible then 'implausible'
                            when is_doubtful then 'doubtful'
                            when not is_precise then 'imprecise'
                            else 'other' end as r
                from marts.fct_stop_events
                where scheduled_arrival < now() - interval '2 hours'
            ) x
            group by 1 order by 1
        """,
        )
        print(
            "Trips never in LTD's feed, by the hour they were due to start (all days): cancelled trips, or buses not reporting"
        )
        show(
            cur,
            """
            select hour_local as trip_start_hour, count(*) trips_not_in_feed
            from marts.fct_stop_events
            where is_first_stop and not trip_had_realtime
              and scheduled_arrival < now() - interval '2 hours'
            group by 1 order by 1
        """,
            limit=30,
        )
        show(
            cur,
            """
            select service_date, count(*) scored,
                   round(100.0 * count(*) filter (where status = 'on_time') / count(*)) pct_on_time,
                   round(100.0 * count(*) filter (where status = 'early') / count(*)) pct_early,
                   round(percentile_cont(0.5) within group (order by delay_s)) median_delay_s,
                   round(percentile_cont(0.5) within group (order by uncertainty_s)) median_unc_s,
                   count(*) filter (where uncertainty_s > 120) unc_over_2min
            from marts.fct_stop_events where status is not null group by 1 order by 1
        """,
        )

        section("DISK: database growth per day, and the biggest tables")
        show(
            cur,
            """
            select day, pg_size_pretty(size) as size_at_end_of_day,
                   pg_size_pretty(size - lag(size) over (order by day)) as growth
            from (
                select (finished_at at time zone 'America/Los_Angeles')::date as day,
                       max(db_bytes) filter (where finished_at = last) as size
                from (select *, max(finished_at) over (partition by
                        (finished_at at time zone 'America/Los_Angeles')::date) as last
                      from analytics.build_log where db_bytes is not null) b
                group by 1
            ) d order by day desc limit 14
        """,
        )
        show(
            cur,
            """
            select n.nspname || '.' || c.relname as table_name,
                   pg_size_pretty(pg_total_relation_size(c.oid)) as total_size
            from pg_class c join pg_namespace n on n.oid = c.relnamespace
            where c.relkind in ('r', 'm') and n.nspname not in ('pg_catalog', 'information_schema')
            order by pg_total_relation_size(c.oid) desc limit 12
        """,
        )

        section(
            "BETWEEN-TIMEPOINT CHECK: is our lateness at minor stops in line with the timepoints around it?"
        )
        print(
            "bump = lateness at a minor stop − the average of the timepoints just before and after it on\n"
            "the same trip (last 3 days). Near 0: our minor-stop times are consistent with the timepoints,\n"
            "where LTD's own times agree with ours, so LTD's times at minor stops are the ones that are off.\n"
            "Around +50 s (the gap to LTD's minor-stop times in METHOD AGREEMENT below): ours run late."
        )
        show(
            cur,
            """
            with e as (
                select service_date, trip_id, stop_sequence, is_timepoint, delay_s
                from marts.fct_stop_events
                where service_date >= current_date - 3 and status is not null
            ),
            g as (
                select *,
                       count(*) filter (where is_timepoint) over (
                           partition by service_date, trip_id order by stop_sequence) as up,
                       count(*) filter (where is_timepoint) over (
                           partition by service_date, trip_id order by stop_sequence desc) as down
                from e
            ),
            a as (
                select *,
                       first_value(delay_s) over (
                           partition by service_date, trip_id, up order by stop_sequence) as prev_tp,
                       first_value(delay_s) over (
                           partition by service_date, trip_id, down order by stop_sequence desc) as next_tp
                from g
            )
            select count(*) minor_stops,
                   round(percentile_cont(0.5) within group (order by delay_s - (prev_tp + next_tp) / 2.0)) median_bump_s,
                   round(percentile_cont(0.25) within group (order by delay_s - (prev_tp + next_tp) / 2.0)) p25_s,
                   round(percentile_cont(0.75) within group (order by delay_s - (prev_tp + next_tp) / 2.0)) p75_s
            from a
            where not is_timepoint and up > 0 and down > 0
        """,
        )
        print(
            "The same, by how the timepoints either side were timed (ltd_departure = LTD's own\n"
            "recorded departure, gps = ours). If the bump is near 0 between GPS-timed timepoints and\n"
            "large between LTD-timed ones, the two ways of timing catch different moments (the bus\n"
            "passing or pulling away, against LTD's departure), not the bus running late."
        )
        show(
            cur,
            """
            with e as (
                select service_date, trip_id, stop_sequence, is_timepoint, delay_s, time_source
                from marts.fct_stop_events
                where service_date >= current_date - 3 and status is not null
            ),
            g as (
                select *,
                       count(*) filter (where is_timepoint) over (
                           partition by service_date, trip_id order by stop_sequence) as up,
                       count(*) filter (where is_timepoint) over (
                           partition by service_date, trip_id order by stop_sequence desc) as down
                from e
            ),
            a as (
                select *,
                       first_value(delay_s) over w_up as prev_tp,
                       first_value(time_source) over w_up as prev_src,
                       first_value(delay_s) over w_down as next_tp,
                       first_value(time_source) over w_down as next_src
                from g
                window w_up as (partition by service_date, trip_id, up order by stop_sequence),
                       w_down as (partition by service_date, trip_id, down order by stop_sequence desc)
            )
            select prev_src, next_src, count(*) minor_stops,
                   round(percentile_cont(0.5) within group (order by delay_s - (prev_tp + next_tp) / 2.0)) median_bump_s,
                   round(percentile_cont(0.25) within group (order by delay_s - (prev_tp + next_tp) / 2.0)) p25_s,
                   round(percentile_cont(0.75) within group (order by delay_s - (prev_tp + next_tp) / 2.0)) p75_s
            from a
            where not is_timepoint and up > 0 and down > 0
            group by 1, 2 order by 3 desc
        """,
        )

        section(
            "PREDICTIONS AGAINST HOW THE ARRIVAL WAS TIMED (5 minutes away, last 30 days): is the "
            "bus later than predicted, or do we time a later moment?"
        )
        print(
            "LTD predicts when the bus arrives. At timepoints we score the later of LTD's recorded\n"
            "departure and our GPS time; at other stops our GPS time, when the bus passes or pulls\n"
            "away. If 'later than predicted'\n"
            "is much bigger at other stops than at timepoints, part of it is the moment we time\n"
            "(the stop's dwell), not the bus. signed = median of (our time − predicted), seconds."
        )
        show(
            cur,
            """
            select e.is_timepoint, e.time_source, count(*) as n,
                   round(percentile_cont(0.5) within group (order by c.error_s)) as signed_median_s,
                   round(percentile_cont(0.1) within group (order by c.error_s)) as p10_s,
                   round(percentile_cont(0.9) within group (order by c.error_s)) as p90_s,
                   round(100.0 * avg((c.error_s > 0)::int)) as pct_bus_later
            from marts.fct_countdown_samples c
            join marts.fct_stop_events e
              on e.service_date = c.service_date and e.trip_id = c.trip_id
             and e.stop_sequence = c.stop_sequence
            where c.in_comparison and c.ahead_min = 5
              and c.service_date >= (now() at time zone 'America/Los_Angeles')::date - 29
            group by 1, 2 order by 1, 2
        """,
        )

        section(
            "TIMEPOINTS AND FIRST STOPS: LTD's recorded departure against our GPS time for the same "
            "bus (last 7 days)"
        )
        print(
            "Where both times exist, the site scores the later of the two (each can come early: our\n"
            "GPS can catch a bus still waiting, LTD's record is sometimes just the scheduled time).\n"
            "ltd_minus_gps = LTD's time minus ours; pct_ltd_scored = how often LTD's was the later;\n"
            "the lateness columns are the typical lateness by LTD's time, by ours, and as scored.\n"
            "Scored should sit a little above the later of the two medians, not far above it."
        )
        show(
            cur,
            """
            select is_first_stop, count(*) as n,
                   round(100.0 * avg((time_source = 'ltd_departure')::int), 1) as pct_ltd_scored,
                   round(percentile_cont(0.5) within group (order by methods_diff_s)) as ltd_minus_gps_s,
                   round(percentile_cont(0.25) within group (order by methods_diff_s)) as p25_s,
                   round(percentile_cont(0.75) within group (order by methods_diff_s)) as p75_s,
                   round(100.0 * avg((extract(epoch from feed_settled_time - scheduled_arrival) = 0)::int), 1)
                       as pct_ltd_exactly_scheduled,
                   round(percentile_cont(0.5) within group (order by
                       extract(epoch from feed_settled_time - scheduled_arrival))) as lateness_by_ltd_s,
                   round(percentile_cont(0.5) within group (order by
                       extract(epoch from observed_arrival - scheduled_arrival))) as lateness_by_gps_s,
                   round(percentile_cont(0.5) within group (order by delay_s)) as lateness_scored_s
            from marts.fct_stop_events
            where (is_timepoint or is_first_stop) and status is not null
              and methods_diff_s is not null and feed_settled_time is not null
              and service_date >= (now() at time zone 'America/Los_Angeles')::date - 6
            group by 1 order by 1
        """,
        )

        section("METHOD AGREEMENT (feed settled − geometric): timepoints are the fair test")
        show(
            cur,
            """
            select is_timepoint, count(*) n,
                   round(percentile_cont(0.5) within group (order by methods_diff_s)) median_diff_s,
                   round(percentile_cont(0.5) within group (order by abs(methods_diff_s))) median_abs_s,
                   round(100.0 * count(*) filter (where abs(methods_diff_s) <= 60) / count(*)) pct_within_1min,
                   count(*) filter (where abs(methods_diff_s) > 300) over_5min
            from marts.fct_stop_events where methods_diff_s is not null and is_bounded
            group by 1 order by 1
        """,
        )
        show(
            cur,
            """
            select route_short_name route, count(*) n, count(*) filter (where abs(methods_diff_s) > 300) over_5min,
                   round(100.0 * count(*) filter (where abs(methods_diff_s) > 300) / count(*)) pct,
                   round(100.0 * count(*) filter (where abs(methods_diff_s) > 300 and service_date >= current_date - 1)
                         / nullif(count(*) filter (where service_date >= current_date - 1), 0)) pct_last_2_days,
                   round(percentile_cont(0.5) within group (order by methods_diff_s)) median_diff_s,
                   round(percentile_cont(0.5) within group (order by methods_diff_s) filter (where is_timepoint)) median_diff_timepoints_s
            from marts.fct_stop_events where methods_diff_s is not null and is_bounded
            group by 1 having count(*) >= 50 order by pct desc limit 8
        """,
        )
        print(
            "Stops the arrival method can't place on the route shape (out of order, or > 60 m off it), by route"
        )
        show(
            cur,
            """
            select r.route_short_name route, count(*) stop_times,
                   count(*) filter (where not f.is_usable) unusable,
                   round(100.0 * count(*) filter (where not f.is_usable) / count(*), 1) pct_unusable
            from intermediate.int_stop_shape_fractions f
            join gtfs.trips t on t.trip_id = f.trip_id and t.feed_version_id = f.feed_version_id
            join gtfs.routes r on r.route_id = t.route_id and r.feed_version_id = t.feed_version_id
            where f.feed_version_id = (select max(feed_version_id) from gtfs.feed_version)
            group by 1 having count(*) filter (where not f.is_usable) > 0 order by 4 desc limit 10
        """,
        )

        section(
            "CALIBRATION, per prediction row (how the site measured the countdown before this fix)"
        )
        show(
            cur,
            """
            select horizon_min, n_predictions n, n_days,
                   round(100.0 * n_within_1min / n_predictions) pct_within_1min,
                   round(100.0 * n_within_2min / n_predictions) pct_within_2min,
                   round(median_error_s) median_err_s,
                   round(100.0 * n_schedule_within_1min / n_predictions) timetable_within_1min
            from marts.mart_calibration where route_id is null and horizon_min in (1, 3, 5, 10, 15, 20, 30, 45, 60)
            order by 1
        """,
        )

        section("COUNTDOWN: WHICH BUSES GET COUNTED (prediction rows vs one count per bus arrival)")
        print(
            "Per row of fct_prediction_errors = one value LTD sent (a bus whose estimate keeps\n"
            "changing sends many). Per arrival = fct_countdown_samples, what the countdown showed\n"
            "while it read that many minutes, once per bus arrival (what the site now uses), on\n"
            "arrivals that had a countdown at 15 min. timetable_all = every scored arrival."
        )
        show(
            cur,
            """
            with rows_way as (
                select horizon_min h, count(*) n_rows,
                       count(distinct (service_date, trip_id, stop_sequence)) n_arrivals,
                       round(100.0 * avg((abs(error_s) <= 60)::int)) countdown_rows,
                       round(100.0 * avg((abs(extract(epoch from observed_arrival
                                                          - scheduled_arrival)) <= 60)::int))
                           timetable_same_rows
                from marts.fct_prediction_errors where horizon_min in (1, 3, 5, 10, 15)
                group by 1
            ),
            arrival_way as (
                select ahead_min h, count(*) n_arrivals_cmp,
                       round(100.0 * avg((abs(error_s) <= 60)::int)) countdown_per_arrival
                from marts.fct_countdown_samples
                where in_comparison and ahead_min in (1, 3, 5, 10, 15) group by 1
            ),
            tt_cmp as (
                select round(100.0 * avg((abs(schedule_error_s) <= 60)::int)) tt
                from marts.fct_countdown_samples where in_comparison and ahead_min = 15
            ),
            tt_all as (
                select round(100.0 * avg((abs(delay_s) <= 60)::int)) tt
                from marts.fct_stop_events where status is not null
            )
            select r.h minutes_ahead, r.n_rows, r.n_arrivals,
                   round(1.0 * r.n_rows / nullif(r.n_arrivals, 0), 1) rows_per_arrival,
                   r.countdown_rows, r.timetable_same_rows,
                   a.countdown_per_arrival, (select tt from tt_cmp) timetable_same_arrivals,
                   (select tt from tt_all) timetable_all
            from rows_way r left join arrival_way a using (h) order by 1
        """,
        )
        print(
            "Timetable right to within 1 min, for arrivals grouped by how many values LTD sent\n"
            "for them while 1 to 15 minutes away (if more values go with a worse timetable, counting\n"
            "rows over-weights the buses that are off schedule):"
        )
        show(
            cur,
            """
            with per as (
                select service_date, trip_id, stop_sequence, count(*) n_values,
                       max(abs(extract(epoch from observed_arrival - scheduled_arrival))) err
                from marts.fct_prediction_errors where horizon_min between 1 and 15
                group by 1, 2, 3
            )
            select case when n_values <= 2 then '1-2' when n_values <= 5 then '3-5'
                        when n_values <= 10 then '6-10' else '11+' end values_sent,
                   count(*) arrivals,
                   round(100.0 * avg((err <= 60)::int)) timetable_within_1min_pct
            from per group by 1 order by min(n_values)
        """,
        )

        section("CHART CHECKS: numbers on the site recomputed straight from the base tables")
        show(
            cur,
            """
            with today as (select (now() at time zone 'America/Los_Angeles')::date d),
            site as (
                select median_delay_s, p10_delay_s, p90_delay_s, n from marts.mart_lateness
                where period = '30d' and days = 'all' and route_id is null and stop_id is null
                  and hour_local is null
            ),
            direct as (
                select percentile_cont(0.5) within group (order by delay_s) median_delay_s,
                       percentile_cont(0.1) within group (order by delay_s) p10_delay_s,
                       percentile_cont(0.9) within group (order by delay_s) p90_delay_s,
                       count(*) n
                from marts.fct_stop_events
                where status is not null and service_date >= (select d - 29 from today)
            )
            select 'Overview, all routes, last 30 days' chart,
                   round(s.median_delay_s) site_typical_s, round(d.median_delay_s) direct_typical_s,
                   round(s.p10_delay_s) || ' to ' || round(s.p90_delay_s) site_8in10_s,
                   round(d.p10_delay_s) || ' to ' || round(d.p90_delay_s) direct_8in10_s,
                   s.n site_n, d.n direct_n,
                   case when abs(s.median_delay_s - d.median_delay_s) <= 1 and s.n = d.n
                        then 'PASS' else 'DIFFERENT (the mart is up to an hour old)' end result
            from site s, direct d
        """,
        )
        show(
            cur,
            """
            with site as (
                select ahead_min, sum(n) n, sum(n_within_1min) n_within_1min,
                       sum(sum_abs_s)::float8 / sum(n) mean_abs_s
                from marts.mart_prediction_daily group by 1
            ),
            direct as (
                select null::int ahead_min, count(*) n,
                       count(*) filter (where abs(schedule_error_s) <= 60) n_within_1min,
                       avg(abs(schedule_error_s)) mean_abs_s
                from marts.fct_countdown_samples where in_comparison and ahead_min = 15
                union all
                select ahead_min, count(*), count(*) filter (where abs(error_s) <= 60),
                       avg(abs(error_s))
                from marts.fct_countdown_samples where in_comparison group by 1
            )
            select coalesce(d.ahead_min::text, 'timetable') countdown_at,
                   round(s.mean_abs_s) site_avg_off_s, round(d.mean_abs_s) direct_avg_off_s,
                   round(100.0 * s.n_within_1min / s.n) site_within_1min_pct,
                   round(100.0 * d.n_within_1min / d.n) direct_within_1min_pct,
                   s.n site_n, d.n direct_n,
                   case when s.n = d.n and s.n_within_1min = d.n_within_1min
                         and abs(s.mean_abs_s - d.mean_abs_s) < 0.5 then 'PASS'
                        else 'DIFFERENT (the mart is up to an hour old)' end result
            from direct d left join site s on s.ahead_min is not distinct from d.ahead_min
            where d.ahead_min is null or d.ahead_min in (1, 2, 3, 5, 10, 15)
            order by d.ahead_min nulls first
        """,
        )
        print(
            "Prediction average minutes off, per route (timetable = the same arrivals' average "
            "off the timetable):"
        )
        show(
            cur,
            """
            select r.route_short_name route,
                   round(avg(abs(c.schedule_error_s)) filter (where c.ahead_min = 15)) timetable_s,
                   round(avg(abs(c.error_s)) filter (where c.ahead_min = 1)) at_1_s,
                   round(avg(abs(c.error_s)) filter (where c.ahead_min = 5)) at_5_s,
                   round(avg(abs(c.error_s)) filter (where c.ahead_min = 10)) at_10_s,
                   round(avg(abs(c.error_s)) filter (where c.ahead_min = 15)) at_15_s,
                   count(*) filter (where c.ahead_min = 15) arrivals
            from marts.fct_countdown_samples c
            join gtfs.routes r on r.route_id = c.route_id
             and r.feed_version_id = (select max(feed_version_id) from gtfs.feed_version)
            where c.in_comparison
            group by 1 order by length(r.route_short_name), 1
        """,
        )
        print(
            "Report cards' predictions (mart_prediction_stop_daily, by stop) against the\n"
            "Predictions page's (mart_prediction_daily), last 30 days: the same arrivals, so the\n"
            "same counts and averages:"
        )
        show(
            cur,
            """
            with a as (
                select basis, coalesce(ahead_min, 0) m, sum(n) n, sum(sum_abs_s) s
                from marts.mart_prediction_daily where service_date >= current_date - 30
                group by 1, 2
            ), b as (
                select basis, coalesce(ahead_min, 0) m, sum(n) n, sum(sum_abs_s) s
                from marts.mart_prediction_stop_daily where service_date >= current_date - 30
                group by 1, 2
            )
            select a.basis, nullif(a.m, 0) ahead_min, a.n page_n, b.n report_card_n,
                   round(a.s::numeric / a.n) page_avg_s, round(b.s::numeric / b.n) report_card_avg_s,
                   case when a.n = b.n and a.s = b.s then 'PASS' else 'DIFFERENT' end result
            from a left join b using (basis, m)
            where a.m in (0, 1, 5, 10, 15) order by 1, 2
        """,
        )

        section("OUTLIER STOPS: typically over 5 min late, or 8 in 10 starting over 5 min early")
        print(
            "Last 30 days, stops with 20+ arrivals. timepoint/first/last = share of the stop's\n"
            "arrivals that are a timepoint / a trip's first stop / its last stop. methods_* compare\n"
            "our GPS time with LTD's own time for the same arrival (LTD's is a real measurement at\n"
            "timepoints only): a big gap where it's measured points at our method, not the bus."
        )
        show(
            cur,
            """
            select e.stop_id, max(s.stop_name) stop_name, string_agg(distinct e.route_short_name, ',') routes,
                   count(*) n, round(percentile_cont(0.5) within group (order by e.delay_s)) median_s,
                   round(percentile_cont(0.1) within group (order by e.delay_s)) p10_s,
                   round(percentile_cont(0.9) within group (order by e.delay_s)) p90_s,
                   round(100.0 * avg(e.is_timepoint::int)) timepoint_pct,
                   round(100.0 * avg(e.is_first_stop::int)) first_pct,
                   round(100.0 * avg(e.is_last_stop::int)) last_pct,
                   round(percentile_cont(0.5) within group (order by e.methods_diff_s)) methods_median_s,
                   round(100.0 * count(*) filter (where abs(e.methods_diff_s) > 120)
                         / nullif(count(e.methods_diff_s), 0)) methods_over_2min_pct
            from marts.fct_stop_events e
            left join gtfs.stops s on s.stop_id = e.stop_id
             and s.feed_version_id = (select max(feed_version_id) from gtfs.feed_version)
            where e.status is not null and e.service_date >= current_date - 30
            group by 1
            having count(*) >= 20
               and (percentile_cont(0.5) within group (order by e.delay_s) > 300
                    or percentile_cont(0.1) within group (order by e.delay_s) < -300)
            order by abs(percentile_cont(0.5) within group (order by e.delay_s)) desc
            limit 25
        """,
        )
        print("How many stops are outliers, and how early arrivals split by kind of stop:")
        show(
            cur,
            """
            select case when is_first_stop then 'first stop' when is_last_stop then 'last stop'
                        when is_timepoint then 'timepoint' else 'other stop' end kind,
                   time_source, count(*) n,
                   round(100.0 * count(*) filter (where delay_s = 0) / count(*), 1) pct_exactly_0s,
                   round(100.0 * count(*) filter (where delay_s < -180) / count(*), 1) pct_over_3min_early,
                   round(100.0 * count(*) filter (where delay_s < -300) / count(*), 1) pct_over_5min_early,
                   round(100.0 * count(*) filter (where delay_s > 300) / count(*), 1) pct_over_5min_late
            from marts.fct_stop_events
            where status is not null and service_date >= current_date - 30
            group by 1, 2 order by 1, 2
        """,
        )
        print(
            "First stops: of the trips we saw running, how many have an LTD departure we score\n"
            "(time_source), and how long before LTD's departure our GPS saw the bus arrive:"
        )
        show(
            cur,
            """
            select count(*) n_first_stops_seen,
                   round(100.0 * avg((time_source = 'ltd_departure')::int), 1) pct_ltd_departure,
                   round(100.0 * avg((status is not null)::int), 1) pct_scored,
                   round(percentile_cont(0.5) within group
                         (order by extract(epoch from feed_settled_time - observed_arrival))) arrived_before_departure_median_s,
                   round(percentile_cont(0.9) within group
                         (order by extract(epoch from feed_settled_time - observed_arrival))) arrived_before_departure_p90_s
            from marts.fct_stop_events
            where is_first_stop and trip_had_realtime and service_date >= current_date - 30
        """,
        )
        print(
            "Which condition keeps a first stop off LTD's departure (each column: first stops of\n"
            "trips we saw that pass it; was_skipped as the feed table stores it):"
        )
        show(
            cur,
            """
            select count(*) n,
                   count(*) filter (where f.settled_time is not null) has_ltd_time,
                   count(*) filter (where f.is_settled) settled,
                   count(*) filter (where f.was_skipped) skipped_true,
                   count(*) filter (where f.was_skipped = false) skipped_false,
                   count(*) filter (where f.settled_time is not null and f.was_skipped is null)
                       skipped_null,
                   count(*) filter (where abs(extract(epoch from f.settled_time - e.scheduled_arrival))
                                    <= 1800) within_30min_of_schedule
            from marts.fct_stop_events e
            left join intermediate.int_feed_settled_times f
              on f.service_date = e.service_date and f.trip_id = e.trip_id
             and f.stop_sequence = e.stop_sequence
            where e.is_first_stop and e.trip_had_realtime and e.service_date >= current_date - 7
        """,
        )

        section("ARRIVAL OR DEPARTURE? which moment our GPS time catches, at timepoints")
        print(
            "At timepoints LTD records when the bus left. If our time caught the bus arriving\n"
            "(and it then waited, as buses do at timepoints when early), LTD's time would be later\n"
            "than ours: ltd_later_1min_pct would be large. Last 7 days; diff = LTD's − ours."
        )
        show(
            cur,
            """
            select count(*) n,
                   round(percentile_cont(0.5) within group (order by methods_diff_s)) median_diff_s,
                   round(100.0 * avg((methods_diff_s > 60)::int), 1) ltd_later_1min_pct,
                   round(100.0 * avg((methods_diff_s < -60)::int), 1) ltd_earlier_1min_pct,
                   round(100.0 * avg((methods_diff_s > 60)::int)
                         filter (where observed_arrival < scheduled_arrival - interval '60 seconds'), 1)
                       ltd_later_1min_pct_when_gps_says_early
            from marts.fct_stop_events
            where is_timepoint and methods_diff_s is not null and is_bounded
              and service_date >= current_date - 7
        """,
        )

        section("EARLY BUSES BETWEEN TIMEPOINTS: real, or the timetable's guessed times?")
        print(
            "LTD's timetable fixes times only at timepoints; times at the stops between are\n"
            "estimates spread evenly by distance (as trip planners do). Minor stops split by where\n"
            "they fall between the timepoints before and after (by scheduled time). If early buses\n"
            "pile up in the last third and are on time at the next timepoint, they ran ahead of the\n"
            "estimate and then waited at the timepoint: early against a guessed time, not a\n"
            "promised one. Last 7 days, scored arrivals."
        )
        show(
            cur,
            """
            with e as (
                select service_date, trip_id, stop_sequence, is_timepoint, is_first_stop,
                       scheduled_arrival, delay_s
                from marts.fct_stop_events
                where status is not null and service_date >= current_date - 7
            ),
            w as (
                select e.*,
                       max(case when is_timepoint then stop_sequence end) over (
                           partition by service_date, trip_id order by stop_sequence
                           rows between unbounded preceding and 1 preceding) as prev_tp,
                       min(case when is_timepoint then stop_sequence end) over (
                           partition by service_date, trip_id order by stop_sequence
                           rows between 1 following and unbounded following) as next_tp
                from e
            ),
            j as (
                select w.*, p.scheduled_arrival as prev_sched, n.scheduled_arrival as next_sched,
                       n.delay_s as next_delay_s
                from w
                join e p on p.service_date = w.service_date and p.trip_id = w.trip_id
                        and p.stop_sequence = w.prev_tp
                join e n on n.service_date = w.service_date and n.trip_id = w.trip_id
                        and n.stop_sequence = w.next_tp
                where not w.is_timepoint and not w.is_first_stop
                  and n.scheduled_arrival > p.scheduled_arrival
            ),
            b as (
                select *, case
                    when extract(epoch from scheduled_arrival - prev_sched)
                         / extract(epoch from next_sched - prev_sched) < 1.0 / 3 then '1 first third'
                    when extract(epoch from scheduled_arrival - prev_sched)
                         / extract(epoch from next_sched - prev_sched) < 2.0 / 3 then '2 middle third'
                    else '3 last third' end as place
                from j
            )
            select place, count(*) n,
                   round(percentile_cont(0.5) within group (order by delay_s)) median_s,
                   round(100.0 * avg((delay_s < -120)::int), 1) over_2min_early_pct,
                   round(100.0 * avg((delay_s < -120 and next_delay_s >= -60)::int), 1)
                       early_here_on_time_at_next_tp_pct,
                   round(percentile_cont(0.5) within group (order by next_delay_s)) next_tp_median_s
            from b group by 1
            union all
            select '0 timepoints', count(*),
                   round(percentile_cont(0.5) within group (order by delay_s)),
                   round(100.0 * avg((delay_s < -120)::int), 1), null, null
            from e where is_timepoint and not is_first_stop
            order by 1
        """,
        )

        section("WHERE PREDICTIONS LOOK WORSE THAN THE TIMETABLE (5 minutes away, last 30 days)")
        print(
            "Per route, average minutes off (seconds) for the prediction when it said 5 minutes,\n"
            "and the printed timetable on the same arrivals: all stops; timepoints only (where\n"
            "LTD's own times confirm ours); and arrivals where our time and LTD's agree within a\n"
            "minute. If the prediction only loses at minor stops, or only where the two times\n"
            "disagree, the loss is about how arrivals are timed, not the predictions. Routes where\n"
            "predictions lose overall, then the total."
        )
        show(
            cur,
            """
            with c as (
                select e.route_short_name route, e.is_timepoint, e.methods_diff_s,
                       abs(c.error_s) pred, abs(c.schedule_error_s) tt
                from marts.fct_countdown_samples c
                join marts.fct_stop_events e
                  on e.service_date = c.service_date and e.trip_id = c.trip_id
                 and e.stop_sequence = c.stop_sequence
                where c.in_comparison and c.ahead_min = 5 and c.service_date >= current_date - 30
            ),
            r as (
                select coalesce(route, 'ALL') route, count(*) n,
                       round(avg(pred)) pred_s, round(avg(tt)) timetable_s,
                       round(avg(pred) filter (where is_timepoint)) pred_tp_s,
                       round(avg(tt) filter (where is_timepoint)) timetable_tp_s,
                       round(avg(pred) filter (where abs(methods_diff_s) <= 60)) pred_agree_s,
                       round(avg(tt) filter (where abs(methods_diff_s) <= 60)) timetable_agree_s
                from c group by rollup (route)
            )
            select * from r where pred_s > timetable_s or route = 'ALL'
            order by route = 'ALL', pred_s - timetable_s desc
        """,
        )
        print(
            "The same at the stop level: stops (with their route) where the prediction 5 minutes\n"
            "out is furthest off compared with the timetable, 30+ arrivals:"
        )
        show(
            cur,
            """
            select e.route_short_name route, max(s.stop_name) stop_name, bool_or(e.is_timepoint) tp,
                   count(*) n, round(avg(abs(c.error_s))) pred_s,
                   round(avg(abs(c.schedule_error_s))) timetable_s,
                   round(percentile_cont(0.5) within group (order by e.methods_diff_s)) methods_median_s
            from marts.fct_countdown_samples c
            join marts.fct_stop_events e
              on e.service_date = c.service_date and e.trip_id = c.trip_id
             and e.stop_sequence = c.stop_sequence
            left join gtfs.stops s on s.stop_id = e.stop_id
             and s.feed_version_id = (select max(feed_version_id) from gtfs.feed_version)
            where c.in_comparison and c.ahead_min = 5 and c.service_date >= current_date - 30
            group by e.route_short_name, e.stop_id having count(*) >= 30
            order by avg(abs(c.error_s)) - avg(abs(c.schedule_error_s)) desc limit 20
        """,
        )

        section("WHERE OUR GPS METHOD AND LTD DISAGREE MOST: stops on the worst routes")
        show(
            cur,
            """
            with worst as (
                select route_short_name from marts.fct_stop_events
                where methods_diff_s is not null and is_bounded and service_date >= current_date - 30
                group by 1 having count(*) >= 200
                order by avg((abs(methods_diff_s) > 300)::int) desc limit 3
            )
            select e.route_short_name route, e.stop_sequence seq, max(s.stop_name) stop_name,
                   bool_or(e.is_timepoint) timepoint, count(*) n,
                   round(percentile_cont(0.5) within group (order by e.methods_diff_s)) methods_median_s,
                   round(100.0 * avg((abs(e.methods_diff_s) > 120)::int)) over_2min_pct,
                   round(percentile_cont(0.5) within group (order by e.uncertainty_s)) uncertainty_s
            from marts.fct_stop_events e
            join worst w using (route_short_name)
            left join gtfs.stops s on s.stop_id = e.stop_id
             and s.feed_version_id = (select max(feed_version_id) from gtfs.feed_version)
            where e.methods_diff_s is not null and e.is_bounded and e.service_date >= current_date - 30
            group by 1, 2 having count(*) >= 10
            order by 1, abs(percentile_cont(0.5) within group (order by e.methods_diff_s)) desc
        """,
        )

        section("CANCELLED TRIPS: does LTD's feed flag them? (trip updates marked CANCELED)")
        show(
            cur,
            """
            select f.fetched_at::date as fetch_day, count(distinct u.trip_id) trips_marked_cancelled
            from rt.trip_update u join rt.fetch f using (fetch_id)
            where u.schedule_relationship = 3 and f.fetched_at > now() - interval '14 days'
            group by 1 order by 1
        """,
        )

        section("PREDICTIONS 1 MIN AWAY, BY ROUTE: is the bus late, or is our arrival time off?")
        print(
            "A prediction 1 minute away should be close. Where it is far off, compare: at\n"
            "timepoints (where LTD's own times verify ours), and where our two arrival methods\n"
            "agree within a minute. If the error shrinks there, our arrival time is the problem.\n"
            "avg_off_s = average |error|; signed = median error, positive = bus came later."
        )
        show(
            cur,
            """
            select coalesce(e.route_short_name, c.route_id) route, count(*) n,
                   round(avg(abs(c.error_s))) avg_off_s,
                   round(percentile_cont(0.5) within group (order by c.error_s)) median_signed_s,
                   round(avg(abs(c.error_s)) filter (where c.is_timepoint)) avg_off_timepoints_s,
                   round(avg(abs(c.error_s)) filter (where abs(e.methods_diff_s) <= 60))
                       avg_off_methods_agree_s,
                   round(100.0 * count(*) filter (where abs(e.methods_diff_s) > 120)
                         / nullif(count(e.methods_diff_s), 0)) pct_methods_differ_2min
            from marts.fct_countdown_samples c
            left join marts.fct_stop_events e using (service_date, trip_id, stop_sequence)
            where c.ahead_min = 1 and c.in_comparison
            group by 1 having count(*) >= 100
            order by avg_off_s desc limit 10
        """,
        )

        section("CALIBRATION BY STOP TYPE (timepoints = where our arrival time is verified)")
        show(
            cur,
            """
            select horizon_min, is_timepoint, count(*) n,
                   round(percentile_cont(0.5) within group (order by error_s)) median_err_s,
                   round(100.0 * count(*) filter (where abs(error_s) <= 60) / count(*)) pct_within_1min,
                   round(100.0 * count(*) filter (where error_s > 60) / count(*)) pct_bus_later
            from marts.fct_prediction_errors
            where horizon_min in (1, 3, 5, 10, 15, 30)
            group by 1, 2 order by 1, 2
        """,
        )

        section("HONEST COUNTDOWN — out-of-sample check on the latest full day")
        show(
            cur,
            f"""
            with test_day as (
                select max(service_date) d from marts.fct_prediction_errors
                where service_date < ((now() at time zone 'America/Los_Angeles') - interval '3 hours')::date
            ),
            e as (
                select route_id, is_timepoint, {HORIZON_BAND_SQL} band, service_date, error_s
                from marts.fct_prediction_errors where horizon_min between 0 and 30
            ),
            train_route as (
                select route_id, is_timepoint, band, count(*) n,
                       percentile_cont(0.5) within group (order by error_s) p50
                from e where service_date < (select d from test_day) group by 1, 2, 3
            ),
            train_all as (
                select is_timepoint, band,
                       percentile_cont(0.5) within group (order by error_s) p50
                from e where service_date < (select d from test_day) group by 1, 2
            ),
            test as (
                select e.error_s,
                       e.error_s - coalesce(case when tr.n >= 50 then tr.p50 end, ta.p50, 0) adjusted_error_s
                from e
                left join train_route tr using (route_id, is_timepoint, band)
                left join train_all ta using (is_timepoint, band)
                where e.service_date = (select d from test_day)
            )
            select (select d from test_day) test_day, count(*) predictions,
                   round(100.0 * count(*) filter (where abs(error_s) <= 60) / nullif(count(*), 0)) ltd_within_1min_pct,
                   round(100.0 * count(*) filter (where abs(adjusted_error_s) <= 60) / nullif(count(*), 0)) adjusted_within_1min_pct,
                   round(percentile_cont(0.5) within group (order by abs(error_s))) ltd_median_abs_err_s,
                   round(percentile_cont(0.5) within group (order by abs(adjusted_error_s))) adjusted_median_abs_err_s
            from test
        """,
        )
        show(
            cur,
            """
            select count(*) rows, count(*) filter (where route_id is not null and day_part is not null) route_and_time_rows,
                   count(*) filter (where n_predictions >= 50) rows_with_50_plus
            from marts.mart_prediction_error_ranges
        """,
        )

        section("DESTINATION NAMES as LTD writes them (current schedule)")
        show(
            cur,
            """
            select r.route_short_name route, t.direction_id dir, t.trip_headsign raw_headsign, count(*) trips
            from gtfs.trips t join gtfs.routes r using (feed_version_id, route_id)
            where t.feed_version_id = (select max(feed_version_id) from gtfs.feed_version)
            group by 1, 2, 3 order by length(r.route_short_name), 1, 2, 4 desc
        """,
            limit=120,
        )

        section("BOARD BALANCE — stop events 5–35 min ago recognised as 'left'")
        show(
            cur,
            """
            with ev as (
                select coalesce(p.arrival_time, p.departure_time) as t, p.last_seen_at,
                       (select x.last_seen_at from rt.prediction_current x
                        where x.trip_id = p.trip_id and x.start_date = p.start_date
                          and x.stop_sequence > p.stop_sequence
                        order by x.stop_sequence limit 1) as next_seen
                from rt.prediction_current p
                where coalesce(p.arrival_time, p.departure_time)
                      between now() - interval '35 minutes' and now() - interval '5 minutes'
            )
            select count(*) stop_events,
                   count(*) filter (where last_seen_at >= t + interval '20 seconds') old_rule_left,
                   count(*) filter (where last_seen_at >= t + interval '20 seconds'
                                       or next_seen >= t + interval '20 seconds') new_rule_left,
                   count(*) filter (where next_seen is null) last_stop_of_trip
            from ev
        """,
        )

        section("PAGE LOAD TIMES: how long visitors' pages took to draw on the server, last 7 days")
        print(
            "Per page, and whether an analysis build was running then (the database is busiest\n"
            "during builds). From site.page_view.load_ms; visits before it was recorded are empty."
        )
        try:
            show(
                cur,
                """
                select v.page, coalesce(b.during, false) during_build, count(*) n,
                       round((percentile_cont(0.5) within group (order by v.load_ms))::numeric / 1000, 1) median_s,
                       round((percentile_cont(0.9) within group (order by v.load_ms))::numeric / 1000, 1) p90_s,
                       round(max(v.load_ms)::numeric / 1000, 1) slowest_s
                from site.page_view v
                left join lateral (
                    select true as during from analytics.build_log l
                    where v.viewed_at between l.started_at and coalesce(l.finished_at, now()) limit 1
                ) b on true
                where v.device <> 'bot' and v.load_ms is not null
                  and v.viewed_at > now() - interval '7 days'
                group by 1, 2 order by 1, 2
            """,
            )
        except psycopg.Error as e:
            cur.connection.rollback()
            print(f"(not available: {e})")

        coverage_by_hour(settings, cur)

        section(
            "SERVICE ALERTS in LTD's feed now: when each says it applies, and when we first saw it"
        )
        show(
            cur,
            """
            with cur_alerts as (
                select alert_id, payload from rt.alert
                where fetch_id = (select max(fetch_id) from rt.fetch where feed = 'alerts')
            )
            select left(c.payload #>> '{header_text,translation,0,text}', 70) header,
                   (select string_agg(
                        coalesce(to_char(to_timestamp((p ->> 'start')::bigint)
                                 at time zone 'America/Los_Angeles', 'Mon DD'), '?') || ' to ' ||
                        coalesce(to_char(to_timestamp((p ->> 'end')::bigint)
                                 at time zone 'America/Los_Angeles', 'Mon DD'), 'no end'), '; ')
                    from jsonb_array_elements(coalesce(c.payload -> 'active_period', '[]'::jsonb)) p)
                       periods,
                   (select min(f.fetched_at)::date from rt.alert a join rt.fetch f using (fetch_id)
                    where a.alert_id = c.alert_id) first_seen
            from cur_alerts c order by 3
        """,
        )

        section(
            "DOUBTFUL STOPS left out (int_doubtful_stops): our GPS time over 2 min out of line "
            "with the stops either side, as a rule"
        )
        show(
            cur,
            """
            select count(*) stops_left_out,
                   (select count(*) from marts.fct_stop_events
                    where is_doubtful and status is null and service_date >= current_date - 30)
                       arrivals_left_out_30d,
                   (select count(*) from marts.fct_stop_events
                    where status is not null and service_date >= current_date - 30) arrivals_scored_30d
            from intermediate.int_doubtful_stops
        """,
        )
        show(
            cur,
            """
            select r.route_short_name route, d.direction_id dir, s.stop_name, s.stop_code, d.n trips,
                   round(d.median_spike_s::numeric) median_spike_s
            from intermediate.int_doubtful_stops d
            left join gtfs.stops s on s.stop_id = d.stop_id
             and s.feed_version_id = (select max(feed_version_id) from gtfs.feed_version)
            left join gtfs.routes r on r.route_id = d.route_id
             and r.feed_version_id = (select max(feed_version_id) from gtfs.feed_version)
            order by 1, 2, 3
        """,
            limit=60,
        )
        print(
            "The previous rule (LTD's time and ours over 2 min apart as a rule), for comparison: how\n"
            "many stops it flagged, how many of those the new rule flags too, and the spike at the\n"
            "rest. Spikes near 0 there: our time was in line with its neighbours, so LTD's estimate,\n"
            "not our time, was what was off (the reason for the change)."
        )
        show(
            cur,
            """
            with old as (
                select o.route_id, o.direction_id, o.stop_id
                from intermediate.int_observed_arrivals o
                join intermediate.int_feed_settled_times f
                  on f.trip_id = o.trip_id and f.service_date = o.service_date
                 and f.stop_sequence = o.stop_sequence
                where o.is_bounded and f.is_settled and o.service_date >= current_date - 30
                group by 1, 2, 3
                having count(*) >= 20 and abs(percentile_cont(0.5) within group (
                    order by extract(epoch from f.settled_time - o.observed_arrival))) > 120
            ),
            ours as (
                select service_date, trip_id, stop_sequence, route_id, direction_id, stop_id,
                       extract(epoch from observed_arrival - scheduled_arrival) late_s
                from intermediate.int_observed_arrivals
                where is_bounded and is_plausible and service_date >= current_date - 30
                  and not is_first_stop and not is_last_stop
            ),
            spikes as (
                select route_id, direction_id, stop_id,
                       late_s - (lag(late_s) over w + lead(late_s) over w) / 2.0 spike_s
                from ours window w as (partition by service_date, trip_id order by stop_sequence)
            ),
            per_stop as (
                select route_id, direction_id, stop_id,
                       percentile_cont(0.5) within group (order by spike_s) median_spike_s
                from spikes where spike_s is not null group by 1, 2, 3
            )
            select count(*) old_rule_stops,
                   count(*) filter (where n.stop_id is not null) also_new_rule,
                   round(percentile_cont(0.5) within group (order by p.median_spike_s)::numeric)
                       median_spike_s,
                   round(percentile_cont(0.9) within group (order by abs(p.median_spike_s))::numeric)
                       p90_abs_spike_s
            from old o
            left join per_stop p on p.route_id = o.route_id and p.stop_id = o.stop_id
             and p.direction_id is not distinct from o.direction_id
            left join intermediate.int_doubtful_stops n on n.route_id = o.route_id
             and n.stop_id = o.stop_id and n.direction_id is not distinct from o.direction_id
        """,
        )

        route_order_check(conn, cur)
        prediction_strips_check(cur)

        section("POSITIONS off the route shape (detours / bad fixes)")
        show(
            cur,
            """
            select service_date, count(*) positions,
                   round(100.0 * count(*) filter (where off_route_m > 100) / count(*), 1) pct_over_100m
            from intermediate.int_positions_along_shape group by 1 order by 1
        """,
        )

    # Inside a container (`make server-dump`) there is no docker command; the server dump
    # collects the container logs itself instead.
    if shutil.which("docker") is None:
        print("\n(end)")
        return
    section("CONTAINER LOG LINES with ERROR/WARNING/Traceback (last 200 lines each)")
    for svc in ("poller", "scheduler"):
        lines = container_logs(svc)
        print(f"-- {svc}: {len(lines)} lines")
        for ln in lines[-12:]:
            print(ln[:220])
    print("\n(end)")