"""Lightweight scheduler: keeps the analysis layer fresh without human hands.

Runs forever in its own container (docker compose service `scheduler`):
  * every 15 min — `dbt build` (observed arrivals, marts, tests), then
                   `dbt source freshness` (logs a warning if collection has stalled);
                   DBT_BUILD_EVERY_MINUTES changes the interval
  * every day    — reload the static schedule if LTD published a new one,
                   and move stale predictions from rt.prediction_current to history
A loop that checks the clock is all this workload needs; the jobs are plain
functions, so moving them to cron or an orchestrator changes only what calls them.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg

from ltdwatch import gtfs_static
from ltdwatch.config import Settings

log = logging.getLogger(__name__)

DBT_DIR = Path(os.environ.get("DBT_PROJECT_DIR", "/app/dbt"))
MAINTENANCE_SQL = Path(
    os.environ.get("MAINTENANCE_SQL", "/app/sql/maintenance/close_out_stale_predictions.sql")
)


def _dbt(args: list[str], timeout: int) -> subprocess.CompletedProcess:
    env = {**os.environ, "DBT_PROFILES_DIR": str(DBT_DIR)}
    cmd = ["dbt", *args, "--project-dir", str(DBT_DIR), "--no-use-colors"]
    return subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout)


def run_dbt_build(settings: Settings) -> None:
    log.info("dbt build starting")
    result = _dbt(["build"], timeout=3600)
    tail = "\n".join(result.stdout.splitlines()[-15:])
    if result.returncode == 0:
        log.info("dbt build finished\n%s", tail)
    else:
        log.error(
            "dbt build FAILED (exit %s)\n%s\n%s", result.returncode, tail, result.stderr[-2000:]
        )


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
