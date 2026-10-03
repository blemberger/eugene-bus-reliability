"""Diagnostic snapshot: `python -m ltdwatch dump` (or `make dump`, which writes dump.txt).

Collection health, feed shape, what change-only storage saves, the state of the
analysis layer, anomalies, and recent container errors, in about 100 lines.
`--raw` adds the newest raw feed message of each kind.
"""

from __future__ import annotations

import gzip
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import psycopg
from google.protobuf import text_format

from ltdwatch import rt_parse
from ltdwatch.config import Settings

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


def main(settings: Settings, raw: bool = False) -> None:
    print("ltdwatch dump (concise)")
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

        section("CLOCKS & LIVE FRESHNESS (what Live, Board and Overview depend on)")
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
                   n_errors errors, n_warnings warnings
            from analytics.build_log order by started_at desc limit 3
        """,
        )
        show(
            cur,
            """
            select (select count(*) from intermediate.int_scheduled_stop_events) scheduled_events,
                   (select count(*) from intermediate.int_position_fractions) position_fractions,
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
                   stop_events_implausible implausible,
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
        print("first report / implausible time / imprecise time / anything else")
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
                   round(100.0 * count(*) filter (where abs(methods_diff_s) > 300) / count(*)) pct
            from marts.fct_stop_events where methods_diff_s is not null and is_bounded
            group by 1 having count(*) >= 50 order by pct desc limit 6
        """,
        )

        section("CALIBRATION (all routes): how often the sign is right")
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

        section("BUSY STOP BUTTONS (Stops, Live, Predictions): most scored arrivals, last 30 days")
        show(
            cur,
            """
            with scored as (
                select stop_id, count(*) as n from marts.fct_stop_events
                where status is not null and service_date >= current_date - 30 group by 1
            ),
            named as (
                select s.stop_id, s.stop_code, s.stop_name, sc.n,
                       row_number() over (partition by lower(trim(s.stop_name)) order by sc.n desc) as rn
                from scored sc join gtfs.stops s on s.stop_id = sc.stop_id
                 and s.feed_version_id = (select max(feed_version_id) from gtfs.feed_version)
                where s.location_type = 0
                  and s.stop_name not ilike '%%arrival zone%%' and s.stop_name not ilike 'approaching %%'
            )
            select stop_name, stop_id, stop_code, n scored_arrivals from named where rn = 1
            order by n desc limit 9
        """,
        )

        section("POSITIONS off the route shape (detours / bad fixes)")
        show(
            cur,
            """
            select service_date, count(*) positions,
                   round(100.0 * count(*) filter (where off_route_m > 100) / count(*), 1) pct_over_100m
            from intermediate.int_position_fractions group by 1 order by 1
        """,
        )

    section("CONTAINER LOG LINES with ERROR/WARNING/Traceback (last 200 lines each)")
    for svc in ("poller", "scheduler"):
        lines = container_logs(svc)
        print(f"-- {svc}: {len(lines)} lines")
        for ln in lines[-12:]:
            print(ln[:220])
    print("\n(end)")
