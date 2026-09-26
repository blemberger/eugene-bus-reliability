"""Rebuild the database from the raw archive: `python -m ltdwatch replay`.

Walks data/raw/<feed>/YYYY/MM/DD/<header_ts>.pb.gz, in time order, and stores
every message whose header timestamp is not already in rt.fetch. This is what
makes "the database is rebuildable from the archive" true, and it fills any gap
where the poller archived a message but failed to write it (for example, while
the database was missing a column).
"""

from __future__ import annotations

import gzip
import logging
from datetime import UTC, datetime

import psycopg

from ltdwatch import rt_parse
from ltdwatch.config import Settings
from ltdwatch.poller import Poller

log = logging.getLogger(__name__)


TRUNCATE_RT = """
    TRUNCATE rt.alert, rt.prediction_history, rt.prediction_current, rt.trip_update,
             rt.vehicle_position, rt.fetch_unchanged, rt.fetch
    RESTART IDENTITY CASCADE
"""


def main(settings: Settings, since: datetime | None = None, rebuild: bool = False) -> None:
    root = settings.raw_archive_dir
    poller = Poller(settings)
    with psycopg.connect(settings.database_url) as conn:
        if rebuild:
            log.warning("REBUILD: emptying every rt.* table, then replaying the whole archive")
            conn.execute(TRUNCATE_RT)
            conn.commit()
        for feed in ("trip_updates", "vehicle_positions", "alerts"):
            files = sorted((root / feed).rglob("*.pb.gz")) if (root / feed).exists() else []
            with conn.cursor() as cur:
                cur.execute("select header_timestamp from rt.fetch where feed = %s", (feed,))
                have = {r[0] for r in cur.fetchall()}
            stored = skipped = failed = 0
            for path in files:
                header_ts = datetime.fromtimestamp(int(path.name.split(".")[0]), tz=UTC)
                if header_ts in have or (since and header_ts < since):
                    skipped += 1
                    continue
                try:
                    with gzip.open(path, "rb") as f:
                        data = f.read()
                    msg = rt_parse.parse_feed(data)
                    rel = str(path.relative_to(root))
                    if poller.store(conn, feed, data, msg, header_ts, header_ts, rel):
                        stored += 1
                    else:
                        skipped += 1
                    conn.commit()
                except Exception:
                    failed += 1
                    conn.rollback()
                    log.exception("replay failed for %s", path)
                if (stored + failed) % 200 == 0 and stored + failed:
                    log.info("%s: stored %d, skipped %d, failed %d", feed, stored, skipped, failed)
            log.info(
                "%s: done — stored %d, skipped %d (already present), failed %d",
                feed,
                stored,
                skipped,
                failed,
            )
