"""Render every dashboard page headlessly and report what a visitor would see.

Run by `make dump` and `make fetch-dump` (appended to dump.txt), or on its own: python app/selfcheck.py
For each page: exceptions with their traceback, every error / warning / info message,
the size of every table, and what each chart and map contains. Uses Streamlit's own
test runner, the same database (DATABASE_URL) and the same code as `make app`.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
import traceback
from pathlib import Path
from urllib.parse import urlsplit

APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))
# Streamlit warns "no runtime found" for every cached function when run outside a server.
os.environ.setdefault("STREAMLIT_LOGGER_LEVEL", "error")
logging.getLogger("streamlit").setLevel(logging.ERROR)

PAGES = [
    "views/overview.py",
    "views/map.py",
    "views/arrivals.py",
    "views/stops.py",
    "views/routes.py",
    "views/route.py",
    "views/accuracy.py",
    "views/methods.py",
    "views/status.py",
]


def section(title: str) -> None:
    print(f"\n== {title}")


def describe_db() -> None:
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        print("DATABASE_URL is not set (run through `make`, which loads .env)")
        return
    u = urlsplit(url)
    print(
        f"app database: user={u.username} host={u.hostname} port={u.port} db={u.path.lstrip('/')}"
    )


def pick_examples() -> dict:
    """A real route and a real busy stop, so the deeper page states get exercised too."""
    import psycopg

    out = {}
    try:
        with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
            row = conn.execute(
                """
                select t.route_id from rt.prediction_current p
                join gtfs.trips t on t.trip_id = p.trip_id
                where p.last_seen_at > now() - interval '10 minutes'
                group by 1 order by count(*) desc limit 1
                """
            ).fetchone()
            if row is None:
                row = conn.execute("select route_id from gtfs.routes limit 1").fetchone()
            out["route_id"] = row[0] if row else None
            row = conn.execute(
                """
                select st.stop_id from gtfs.stop_times st
                where st.feed_version_id = (select max(feed_version_id) from gtfs.feed_version)
                group by 1 order by count(*) desc limit 1
                """
            ).fetchone()
            out["stop_id"] = row[0] if row else None
    except Exception as exc:  # noqa: BLE001
        print(f"!! could not pick an example route/stop: {exc!r}")
    return out


def report(at, label: str, seconds: float) -> None:
    print(f"\n-- {label}  ({seconds:.1f} s)")
    titles = [t.value for t in at.title]
    if titles:
        print(f"   title: {titles[0]}")
    for e in at.exception:
        print(f"   EXCEPTION: {e.message}")
        for line in (e.stack_trace or [])[-12:]:
            print(f"      {line.rstrip()}")
    for kind in ("error", "warning", "info", "success"):
        for el in getattr(at, kind):
            print(f"   {kind.upper()}: {str(el.value)[:400]}")
    for i, m in enumerate(at.metric):
        print(f"   metric {i}: {m.label} = {m.value}")
    for h in at.get("html"):  # the live countdown (common.live_status_line), as text
        body = getattr(h.proto, "body", "") or ""
        text = " ".join(re.sub(r"<[^>]+>", " ", body).replace("&#9679;", "●").split())
        if "LTD data from" in text or "Live ·" in text or "No new data" in text:
            print(f"   COUNTDOWN: {text}")
    for i, df in enumerate(at.dataframe):
        v = df.value
        print(f"   table {i}: {v.shape[0]} rows × {v.shape[1]} cols {list(v.columns)[:8]}")
    for i, ch in enumerate(at.get("plotly_chart")):
        try:
            spec = json.loads(ch.proto.spec)
            traces = spec.get("data", [])
            npts = []
            for tr in traces:
                x = tr.get("x")
                if isinstance(x, list):
                    npts.append(len(x))
                elif isinstance(x, dict) and "bdata" in x:
                    npts.append("binary")
                else:
                    npts.append(0 if x is None else "?")
            print(f"   chart {i}: {len(traces)} trace(s), points per trace {npts}")
        except Exception as exc:  # noqa: BLE001
            print(f"   chart {i}: could not read ({exc!r})")
    for i, deck in enumerate(at.get("deck_gl_json_chart")):
        try:
            spec = json.loads(deck.proto.json)
            layers = [
                (ly.get("@@type"), len(ly.get("data") or [])) for ly in spec.get("layers", [])
            ]
            print(f"   map {i}: layers (type, rows) {layers}")
        except Exception as exc:  # noqa: BLE001
            print(f"   map {i}: could not read ({exc!r})")


def quiet_streamlit_logs() -> None:
    for name in list(logging.root.manager.loggerDict):
        if name.startswith("streamlit"):
            logging.getLogger(name).setLevel(logging.ERROR)


def run_page(page: str, label: str, state: dict | None = None) -> None:
    from streamlit.testing.v1 import AppTest

    quiet_streamlit_logs()

    import common

    try:
        at = AppTest.from_file(str(APP_DIR / "streamlit_app.py"), default_timeout=120)
        at.run()
        at.switch_page(page)
        for k, v in (state or {}).items():
            at.session_state[k] = v
        # timed from an empty cache: the worst case, a visitor when nobody else has been
        # on the site since the last analysis build
        import streamlit as st

        st.cache_data.clear()
        common.QUERY_LOG.clear()
        t0 = time.monotonic()
        at.run()
        first = time.monotonic() - t0
        queries = sorted(common.QUERY_LOG, reverse=True)
        report(at, label, first)
        # the same page again, as a visitor clicking back to it would get it (cached queries)
        t0 = time.monotonic()
        at.run()
        print(
            f"   speed: {first:.1f} s with nothing cached, {time.monotonic() - t0:.1f} s cached; "
            f"{len(queries)} queries, {sum(q for q, _ in queries):.1f} s in the database"
        )
        for secs, sql in queries[:3]:
            if secs >= 0.2:
                print(f"   slow query {secs:.1f} s: {sql}")
    except Exception:  # noqa: BLE001
        print(f"\n-- {label}: the page test itself failed")
        print("   " + traceback.format_exc().replace("\n", "\n   "))


def stop_names_report() -> None:
    """How LTD's stop names are rewritten for display (common.clean_stop_name), with examples."""
    import psycopg
    from common import stop_name_rule

    section("STOP NAMES as shown on the site (LTD's name -> readable name)")
    try:
        with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
            names = [
                r[0]
                for r in conn.execute(
                    """
                    select distinct stop_name from gtfs.stops
                    where feed_version_id = (select max(feed_version_id) from gtfs.feed_version)
                      and location_type = 0
                    order by 1
                    """
                ).fetchall()
            ]
    except Exception as exc:  # noqa: BLE001
        print(f"!! could not read stop names: {exc!r}")
        return
    by_rule: dict[str | None, list[tuple[str, str]]] = {}
    for n in names:
        new, rule = stop_name_rule(n)
        by_rule.setdefault(rule, []).append((n, new))
    print(f"{len(names)} distinct stop names")
    for rule, pairs in by_rule.items():
        label = rule or "no rule matched (shown as LTD writes it, abbreviations expanded)"
        print(f"\n-- {label}: {len(pairs)}")
        shown = pairs if rule is None else pairs[:: max(1, len(pairs) // 8)][:8]
        for raw, new in shown[:60]:
            print(f"   {raw}  ->  {new}" if raw != new else f"   {raw}")


def main() -> None:
    section("APP SELF-CHECK (every page rendered headlessly, as `make app` would show it)")
    try:
        import streamlit
    except ImportError:
        print("streamlit is not installed here (the app extras); skipped")
        return
    print(f"python {sys.version.split()[0]} · streamlit {streamlit.__version__}")
    describe_db()
    for page in PAGES:
        run_page(page, page)
    stop_names_report()
    ex = pick_examples()
    if ex.get("route_id"):
        run_page(
            "views/arrivals.py",
            f"views/arrivals.py with route {ex['route_id']}",
            {"board_route": ex["route_id"]},
        )
        run_page(
            "views/map.py",
            f"views/map.py with route {ex['route_id']}",
            {"live_route": ex["route_id"]},
        )
        run_page(
            "views/route.py",
            f"views/route.py with route {ex['route_id']}",
            {"route_id": ex["route_id"]},
        )
    if ex.get("stop_id"):
        run_page(
            "views/stops.py",
            f"views/stops.py with stop {ex['stop_id']}",
            {"stop_id": ex["stop_id"]},
        )
        run_page(
            "views/accuracy.py",
            f"views/accuracy.py with stop {ex['stop_id']}",
            {"pred_stop_id": ex["stop_id"]},
        )
        run_page(
            "views/map.py",
            f"views/map.py with stop {ex['stop_id']}",
            {"live_stop_id": ex["stop_id"]},
        )
    print("\n(end of self-check)")


if __name__ == "__main__":
    main()
