"""Command line: python -m eugene_bus_reliability <command>."""

from __future__ import annotations

import argparse
import logging
import sys

from eugene_bus_reliability.config import Settings


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m eugene_bus_reliability")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("load-static", help="download the static GTFS zip and load it if new")
    p.add_argument("--url", help="override GTFS_STATIC_URL")
    p.add_argument("--file", help="load a local zip instead of downloading")

    p = sub.add_parser("poll", help="poll the realtime feeds continuously")
    p.add_argument("--cycles", type=int, help="stop after N cycles (default: run forever)")

    sub.add_parser("report", help="print what has been collected so far")
    sub.add_parser(
        "referrers", help="summarize the web server's access log (on stdin): visits, sources"
    )

    p = sub.add_parser(
        "dump", help="diagnostic snapshot: collection health, feed shape, analysis layer"
    )
    p.add_argument("--raw", action="store_true", help="also print the newest raw feed messages")

    p = sub.add_parser("replay", help="store any archived feed messages missing from the database")
    p.add_argument(
        "--since",
        help="only messages at/after this time, e.g. 2026-09-19T21:00:00 (UTC unless an offset is given)",
    )
    p.add_argument(
        "--rebuild", action="store_true", help="EMPTY all rt.* tables first, then replay everything"
    )

    p = sub.add_parser(
        "schedule", help="run the dbt build (every 15 min) and daily maintenance loop forever"
    )
    p.add_argument("--once", action="store_true", help="run dbt build once and exit (for testing)")

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    settings = Settings.from_env()

    if args.cmd == "load-static":
        import psycopg

        from eugene_bus_reliability import gtfs_static

        with psycopg.connect(settings.database_url) as conn:
            if args.file:
                with open(args.file, "rb") as f:
                    gtfs_static.load_zip(conn, f.read(), f"file://{args.file}")
            else:
                gtfs_static.load_from_url(conn, args.url or settings.gtfs_static_url)

    elif args.cmd == "poll":
        from eugene_bus_reliability.poller import Poller

        Poller(settings).run(max_cycles=args.cycles)

    elif args.cmd == "referrers":
        from eugene_bus_reliability import referrers

        referrers.main()

    elif args.cmd == "report":
        from eugene_bus_reliability import report

        report.run(settings.database_url)

    elif args.cmd == "dump":
        from eugene_bus_reliability import dump

        dump.main(settings, raw=args.raw)

    elif args.cmd == "replay":
        from datetime import UTC, datetime

        from eugene_bus_reliability import replay

        since = None
        if args.since:
            since = datetime.fromisoformat(args.since)
            # a bare time means UTC; a time with an offset is converted, not overwritten
            since = since.replace(tzinfo=UTC) if since.tzinfo is None else since.astimezone(UTC)
        replay.main(settings, since=since, rebuild=args.rebuild)

    elif args.cmd == "schedule":
        from eugene_bus_reliability import scheduler

        if args.once:
            scheduler.run_dbt_build(settings)
        else:
            scheduler.main(settings)
