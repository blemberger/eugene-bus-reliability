"""Lightweight scheduler: keeps the analysis layer fresh without human hands.

Runs forever in its own container (docker compose service `scheduler`):
  * every 15 min — `dbt build` (observed arrivals, marts, tests), then
                   `dbt source freshness` (logs a warning if collection has stalled);
                   DBT_BUILD_EVERY_MINUTES changes the interval. Most models recompute only
                   the latest two service days (dbt/macros/incremental.sql); when the analysis
                   code has changed since the last full refresh, the build is a full refresh
  * every day    — reload the static schedule if LTD published a new one,
                   and move stale predictions from rt.prediction_current to history
A loop that checks the clock is all this workload needs; the jobs are plain
functions, so moving them to cron or an orchestrator changes only what calls them.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg

from eugene_bus_reliability import gtfs_static
from eugene_bus_reliability.config import Settings

log = logging.getLogger(__name__)

DBT_DIR = Path(os.environ.get("DBT_PROJECT_DIR", "/app/dbt"))
MAINTENANCE_SQL = Path(
    os.environ.get("MAINTENANCE_SQL", "/app/sql/maintenance/close_out_stale_predictions.sql")
)


def _dbt(args: list[str], timeout: int) -> subprocess.CompletedProcess:
    env = {**os.environ, "DBT_PROFILES_DIR": str(DBT_DIR)}
    cmd = ["dbt", *args, "--project-dir", str(DBT_DIR), "--no-use-colors"]
    return subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout)


def analysis_code_version() -> str:
    """A fingerprint of everything that decides what the analysis computes: the dbt models,
    macros, tests and project file."""
    h = hashlib.sha256()
    files = [DBT_DIR / "dbt_project.yml"] + [
        p
        for d in ("models", "macros", "tests")
        for p in sorted((DBT_DIR / d).rglob("*"))
        if p.is_file()
    ]
    for p in files:
        h.update(str(p.relative_to(DBT_DIR)).encode() + b"\0" + p.read_bytes() + b"\0")
    return h.hexdigest()[:16]


def refreshed_version(settings: Settings) -> str | None:
    """The code version of the last successful full refresh (None: never, or reset by
    `make server-full-refresh` / the drop-before cleanup, which delete it)."""
    with psycopg.connect(settings.database_url) as conn:
        conn.execute("create schema if not exists analytics")
        conn.execute(
            "create table if not exists analytics.build_code (version text, refreshed_at timestamptz)"
        )
        row = conn.execute(
            "select version from analytics.build_code order by refreshed_at desc limit 1"
        ).fetchone()
    return row[0] if row else None


def models_built_ok() -> bool:
    """Did every model of the last dbt invocation build? (A failing test doesn't count.)"""
    try:
        results = json.loads((DBT_DIR / "target" / "run_results.json").read_text())["results"]
    except (OSError, ValueError, KeyError):
        return False
    return not any(
        r["unique_id"].startswith("model.") and r["status"] != "success" for r in results
    )


def run_dbt_build(settings: Settings) -> None:
    version = analysis_code_version()
    full = refreshed_version(settings) != version
    log.info("dbt build starting%s", " (full refresh: analysis code changed)" if full else "")
    result = _dbt(["build", "--full-refresh"] if full else ["build"], timeout=3600 * 3)
    tail = "\n".join(result.stdout.splitlines()[-15:])
    if result.returncode == 0:
        log.info("dbt build finished\n%s", tail)
    else:
        log.error(
            "dbt build FAILED (exit %s)\n%s\n%s", result.returncode, tail, result.stderr[-2000:]
        )
    if full and models_built_ok():
        with psycopg.connect(settings.database_url) as conn:
            conn.execute("delete from analytics.build_code")
            conn.execute(
                "insert into analytics.build_code (version, refreshed_at) values (%s, now())",
                (version,),
            )


LOCAL_TZ = ZoneInfo("America/Los_Angeles")


def _clock(ts: datetime) -> str:
    return ts.astimezone(LOCAL_TZ).strftime("%-I:%M %p").lower()


def wait_ready(settings: Settings, timeout_minutes: int = 60, poll_seconds: int = 15) -> int:
    """Block until the analysis has been fully rebuilt for the code now deployed (the
    scheduler's full refresh after a code change or `make server-full-refresh`), printing what
    it is doing about once a minute. Returns 0 when ready, 1 if a rebuild failed or on timeout.
    Safe to stop with Ctrl+C at any time: it only watches."""
    version = analysis_code_version()
    began = datetime.now(tz=UTC)
    last_print = None
    while True:
        if refreshed_version(settings) == version:
            with psycopg.connect(settings.database_url) as conn:
                row = conn.execute(
                    "select max(finished_at) from analytics.build_log where full_refresh"
                ).fetchone()
            when = f" (rebuilt at {_clock(row[0])})" if row and row[0] else ""
            print(f"READY: the analysis is up to date with the deployed code{when}.", flush=True)
            return 0
        with psycopg.connect(settings.database_url) as conn:
            running = conn.execute(
                """select started_at from analytics.build_log
                   where finished_at is null and full_refresh and started_at > now() - interval '3 hours'
                   order by started_at desc limit 1"""
            ).fetchone()
            failed = conn.execute(
                """select finished_at, n_errors from analytics.build_log
                   where full_refresh and finished_at > %s and n_errors > 0
                   order by finished_at desc limit 1""",
                (began,),
            ).fetchone()
            finished_since = conn.execute(
                "select 1 from analytics.build_log where full_refresh and finished_at > %s",
                (began,),
            ).fetchone()
            previous = conn.execute(
                """select percentile_cont(0.5) within group (order by extract(epoch from finished_at - started_at))
                   from analytics.build_log where full_refresh and finished_at is not null"""
            ).fetchone()
        if failed:
            print(
                f"FAILED: the full rebuild that finished at {_clock(failed[0])} had {failed[1]} "
                "error(s). Run make fetch-dump and look at LOG LINES.",
                flush=True,
            )
            return 1
        now = datetime.now(tz=UTC)
        if (now - began).total_seconds() > timeout_minutes * 60:
            print(f"GAVE UP after {timeout_minutes} minutes: still not rebuilt.", flush=True)
            return 1
        if last_print is None or (now - last_print).total_seconds() >= 60:
            last_print = now
            typical = (
                f"; a full rebuild usually takes about {round(previous[0] / 60)} min"
                if previous and previous[0]
                else ""
            )
            if running:
                mins = int((now - running[0]).total_seconds() // 60)
                print(
                    f"rebuilding: started {mins} min ago{typical} (checking every {poll_seconds} s)",
                    flush=True,
                )
            elif finished_since:
                print("rebuild finished; recording it...", flush=True)
            else:
                print(f"waiting for the rebuild to start{typical}", flush=True)
        time.sleep(poll_seconds)


def run_source_freshness(settings: Settings) -> None:
    result = _dbt(["source", "freshness"], timeout=300)
    lines = [ln for ln in result.stdout.splitlines() if "freshness of" in ln]
    if result.returncode != 0:
        log.error("source freshness ERROR: collection has stalled\n%s", "\n".join(lines))
    elif any("WARN" in ln for ln in lines):
        log.warning("source freshness WARN: collection is falling behind\n%s", "\n".join(lines))
    else:
        log.info("source freshness ok")


def run_daily(settings: Settings) -> None:
    with psycopg.connect(settings.database_url) as conn:
        log.info("checking for a new static schedule")
        gtfs_static.load_from_url(conn, settings.gtfs_static_url)
        conn.commit()
        if not MAINTENANCE_SQL.exists():
            log.warning("maintenance SQL not found at %s; skipped", MAINTENANCE_SQL)
            return
        conn.execute(MAINTENANCE_SQL.read_text())
        conn.commit()
        log.info("stale predictions moved to history")


def main(settings: Settings, daily_hour_utc: int = 10) -> None:
    """dbt build every DBT_BUILD_EVERY_MINUTES (default 15); daily jobs at startup and at
    daily_hour_utc (10 UTC = 3 am Pacific)."""
    every = timedelta(minutes=int(os.environ.get("DBT_BUILD_EVERY_MINUTES", "15")))
    last_build: datetime | None = None
    last_daily: datetime | None = None
    log.info("scheduler up: dbt build every %s, daily jobs at %02d:00 UTC", every, daily_hour_utc)
    while True:
        now = datetime.now(tz=UTC)
        try:
            # daily jobs: at daily_hour_utc, and once at startup, so a brand-new server loads
            # LTD's schedule immediately instead of waiting for 3 am
            if last_daily is None or (
                now.hour == daily_hour_utc and last_daily.date() != now.date()
            ):
                last_daily = now
                run_daily(settings)
            if last_build is None or now - last_build >= every:
                last_build = now
                run_dbt_build(settings)
                run_source_freshness(settings)
        except Exception:
            log.exception("scheduled job failed")
        time.sleep(30)
